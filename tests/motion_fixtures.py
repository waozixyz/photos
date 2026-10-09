"""Synthetic camera containers; no owner media or credentials."""
from io import BytesIO
from PIL import Image

HEADER = b'http://ns.adobe.com/xap/1.0/\0'


def jpeg(path, clip, *, modern=True, flag=1, length=None, prefix='Camera', padding=0, gainmap=b'', xml=None):
    image = Image.new('RGB', (96, 64), (190, 70, 30))
    exif = image.getexif(); exif[274] = 6
    buffer = BytesIO(); image.save(buffer, 'JPEG', exif=exif)
    still = buffer.getvalue()
    if xml is None:
        length = len(clip) if length is None else length
        flag_name = 'MotionPhoto' if modern else 'MicroVideo'
        attributes = f'{prefix}:{flag_name}="{flag}"'
        if not modern:
            attributes += f' {prefix}:MicroVideoOffset="{length}"'
        directory = ''
        if modern:
            gain = f'<Container:Item Item:Mime="image/jpeg" Item:Semantic="GainMap" Item:Length="{len(gainmap)}"/>' if gainmap else ''
            directory = f'''<Container:Directory><rdf:Seq>
                <rdf:li><Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Padding="{padding}"/></rdf:li>
                {gain}<rdf:li><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{length}"/></rdf:li>
                </rdf:Seq></Container:Directory>'''
        xml = f'''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:{prefix}="http://ns.google.com/photos/1.0/camera/"
            xmlns:Container="http://ns.google.com/photos/1.0/container/"
            xmlns:Item="http://ns.google.com/photos/1.0/container/item/">
            <rdf:RDF><rdf:Description {attributes}>{directory}</rdf:Description></rdf:RDF></x:xmpmeta>'''
    payload = HEADER+xml.encode()
    path.write_bytes(still[:2]+b'\xff\xe1'+(len(payload)+2).to_bytes(2,'big')+payload+still[2:]+b'\0'*padding+gainmap+clip)
    return path


def heif(path, clip, codec=1):
    from codec_fixtures import heic
    mime = 'image/heic' if codec == 1 else 'image/avif'
    xml = f'''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:Camera="http://ns.google.com/photos/1.0/camera/"
        xmlns:Container="http://ns.google.com/photos/1.0/container/"
        xmlns:Item="http://ns.google.com/photos/1.0/container/item/">
        <rdf:RDF><rdf:Description Camera:MotionPhoto="1"><Container:Directory><rdf:Seq>
        <rdf:li><Container:Item Item:Mime="{mime}" Item:Semantic="Primary" Item:Padding="8"/></rdf:li>
        <rdf:li><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{len(clip)}"/></rdf:li>
        </rdf:Seq></Container:Directory></rdf:Description></rdf:RDF></x:xmpmeta>'''.encode()
    heic(path,6,xmp=xml,codec=codec)
    with path.open('ab') as output:
        output.write((len(clip)+8).to_bytes(4,'big')+b'mpvd'+clip)
    return path


def samsung(path,clip):
    import struct
    Image.new('RGB',(96,64),(190,70,30)).save(path)
    data = struct.pack('<HHI',0,0x0a30,16)+b'MotionPhoto_Data'+clip
    directory = b'SEFH'+struct.pack('<IIHHII',101,1,0,0x0a30,len(data),len(data))
    with path.open('ab') as output:
        output.write(data+directory+struct.pack('<I',len(directory))+b'SEFT')
    return path


def extended_jpeg(path, clip, *, reverse=True, split=True, elements=True, mutate=None):
    """Real APP1 chunks with a digest-linked standard packet and split properties."""
    import hashlib
    from xml.etree import ElementTree as ET
    jpeg(path,clip)
    original = path.read_bytes()
    length = int.from_bytes(original[4:6],'big')
    xml = original[6+len(HEADER):4+length].decode()
    if split:
        xml = xml.replace('Camera:MotionPhoto="1"','')
    if elements:
        import re
        xml = re.sub(r'<Container:Item ([^>]+)/>',lambda m:
            '<Container:Item>'+''.join(f'<Item:{key}>{value}</Item:{key}>' for key,value in
                re.findall(r'Item:(\w+)="([^"]*)"',m[1]))+'</Container:Item>',xml)
    # More than one APP1 segment, including a non-motion property.
    xml = xml.replace('</rdf:Description>','<padding xmlns="urn:fixture">'+'x'*70000+'</padding></rdf:Description>')
    extension = xml.encode()
    guid = hashlib.md5(extension).hexdigest().upper().encode()
    standard = f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:N="http://ns.adobe.com/xmp/note/" xmlns:C="http://ns.google.com/photos/1.0/camera/"><rdf:Description N:HasExtendedXMP="{guid.decode()}"'+(' C:MotionPhoto="1"' if split else '')+'/></rdf:RDF>'
    header = b'http://ns.adobe.com/xmp/extension/\0'
    chunks = [header+guid+len(extension).to_bytes(4,'big')+at.to_bytes(4,'big')+extension[at:at+60000]
              for at in range(0,len(extension),60000)]
    if reverse:
        chunks.reverse()
    if mutate:
        standard,chunks = mutate(standard,chunks)
    def app1(payload):
        return b'\xff\xe1'+(len(payload)+2).to_bytes(2,'big')+payload
    path.write_bytes(original[:2]+b''.join(app1(chunk) for chunk in chunks)+app1(HEADER+standard.encode())+original[4+length:])
    return path


def samsung_heif(path, clip, *, xmp=True, version=107):
    """Samsung mpvd contains video then sefd with an absolute mpv2 pointer."""
    import struct
    from codec_fixtures import heic
    def tag(kind,name,data):
        return struct.pack('<HHI',0,kind,len(name))+name.encode()+data
    def trailer(offset):
        blocks = [tag(0xa31,'MotionPhoto_Version',b'mpv3'),
                  tag(0xa30,'MotionPhoto_Data',b'mpv2'+struct.pack('>II',offset,len(clip)))]
        directory = b'SEFH'+struct.pack('<II',version,len(blocks))
        for index,block in enumerate(blocks):
            directory += struct.pack('<HHII',0,[0xa31,0xa30][index],sum(map(len,blocks[index:])),len(block))
        data = b''.join(blocks)+directory+struct.pack('<I',len(directory))+b'SEFT'
        return struct.pack('>I',len(data)+8)+b'sefd'+data
    payload_size = len(clip)+len(trailer(0))
    xml = f'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:C="http://ns.google.com/photos/1.0/camera/" xmlns:Container="http://ns.google.com/photos/1.0/container/"
        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"><rdf:Description C:MotionPhoto="1">
        <Container:Directory><rdf:Seq><rdf:li><Container:Item Item:Mime="image/heic" Item:Semantic="Primary" Item:Padding="8"/></rdf:li>
        <rdf:li><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="{payload_size}"/></rdf:li>
        </rdf:Seq></Container:Directory></rdf:Description></rdf:RDF>'''.encode() if xmp else None
    heic(path,6,xmp=xml)
    offset = path.stat().st_size+8
    with path.open('ab') as output:
        output.write(struct.pack('>I',payload_size+8)+b'mpvd'+clip+trailer(offset))
    return path
