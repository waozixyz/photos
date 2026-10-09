"""Small synthetic camera files; no owner's media or network downloads."""
import ctypes as c
import struct

from PIL import Image, TiffImagePlugin


def dng(path, orientation=1):
    width, height = 96, 64
    data = b''.join(struct.pack('<H', 1000 + x * 100 + y * 40)
                    for y in range(height) for x in range(width))
    image = Image.frombytes('I;16', (width, height), data)
    tags = TiffImagePlugin.ImageFileDirectory_v2()
    values = {262: 32803, 274: orientation, 33421: (2, 2), 33422: b'\x00\x01\x01\x02',
              50706: b'\x01\x04\x00\x00', 50707: b'\x01\x01\x00\x00',
              50708: 'Photos synthetic camera', 50710: b'\x00\x01\x02', 50711: 1,
              50714: 0, 50717: 16383, 50721: (1, 0, 0, 0, 1, 0, 0, 0, 1),
              50728: (1, 1, 1), 50778: 21}
    for key, value in values.items():
        tags[key] = value
    tags.tagtype[33421] = 3
    for tag in [33422, 50706, 50707, 50710]:
        tags.tagtype[tag] = 1
    for tag in [50721, 50728]:
        tags.tagtype[tag] = 10 if tag == 50721 else 5
    image.save(path, format='TIFF', tiffinfo=tags)


def heic(path, orientation=1, xmp=None, codec=1, exif=None):
    class Error(c.Structure):
        _fields_ = [('code', c.c_int), ('subcode', c.c_int), ('message', c.c_char_p)]
    class Options(c.Structure):
        _fields_ = [('version', c.c_uint8), ('alpha', c.c_uint8), ('compatibility', c.c_uint8),
                    ('two_profiles', c.c_uint8), ('profile', c.c_void_p),
                    ('compatibility_no_profile', c.c_uint8), ('orientation', c.c_int)]
    h = c.CDLL('libheif.so.1')
    def bind(name, result, *arguments):
        method = getattr(h, name)
        method.restype = result
        method.argtypes = arguments
        return method
    ptr = c.c_void_p
    alloc = bind('heif_context_alloc', ptr)
    free = bind('heif_context_free', None, ptr)
    create = bind('heif_image_create', Error, c.c_int, c.c_int, c.c_int, c.c_int, c.POINTER(ptr))
    plane = bind('heif_image_add_plane', Error, ptr, c.c_int, c.c_int, c.c_int, c.c_int)
    pixels = bind('heif_image_get_plane', c.POINTER(c.c_uint8), ptr, c.c_int, c.POINTER(c.c_int))
    encoder = bind('heif_context_get_encoder_for_format', Error, ptr, c.c_int, c.POINTER(ptr))
    parameter = bind('heif_encoder_set_parameter', Error, ptr, c.c_char_p, c.c_char_p)
    lossless = bind('heif_encoder_set_lossless', Error, ptr, c.c_int)
    options = bind('heif_encoding_options_alloc', ptr)
    options_free = bind('heif_encoding_options_free', None, ptr)
    encode = bind('heif_context_encode_image', Error, ptr, ptr, ptr, ptr, c.POINTER(ptr))
    write = bind('heif_context_write_to_file', Error, ptr, c.c_char_p)
    add_xmp = bind('heif_context_add_XMP_metadata', Error, ptr, ptr, ptr, c.c_int)
    add_exif = bind('heif_context_add_exif_metadata', Error, ptr, ptr, ptr, c.c_int)
    release_image = bind('heif_image_release', None, ptr)
    release_encoder = bind('heif_encoder_release', None, ptr)
    release_handle = bind('heif_image_handle_release', None, ptr)
    def check(error):
        if error.code:
            raise AssertionError(error.message.decode())
    context = alloc()
    image, enc, handle = ptr(), ptr(), ptr()
    opts = options()
    try:
        width, height = 96, 64
        check(create(width, height, 1, 11, c.byref(image)))
        check(plane(image, 10, width, height, 8))
        stride = c.c_int()
        memory = pixels(image, 10, c.byref(stride))
        for y in range(height):
            row = bytes(v for x in range(width) for v in
                        (210 if x < 48 else 35, 50 if y < 32 else 175, 80, 255 if x < 48 else 110))
            c.memmove(c.addressof(memory.contents) + y * stride.value, row, len(row))
        check(encoder(context, codec, c.byref(enc)))
        if codec == 1:
            check(parameter(enc, b'x265:pools', b'none'))
            check(parameter(enc, b'x265:frame-threads', b'1'))
        check(lossless(enc, 1))
        c.cast(opts, c.POINTER(Options)).contents.orientation = orientation
        check(encode(context, image, enc, opts, c.byref(handle)))
        if xmp is not None:
            metadata = c.create_string_buffer(xmp)
            check(add_xmp(context,handle,c.cast(metadata,ptr),len(xmp)))
        if exif is not None:
            metadata = c.create_string_buffer(exif)
            check(add_exif(context,handle,c.cast(metadata,ptr),len(exif)))
        check(write(context, str(path).encode()))
    finally:
        if handle: release_handle(handle)
        if enc: release_encoder(enc)
        if image: release_image(image)
        options_free(opts)
        free(context)
