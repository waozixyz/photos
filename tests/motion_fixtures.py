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
