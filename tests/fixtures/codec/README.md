These tiny camera files contain only synthetic pixels. The existing
`tests/codec_fixtures.py` generator created the HEIC/AVIF alpha pattern and DNG
sensor ramp, including orientation 6 variants. Native Ziran tests read these
frozen binary fixtures directly; fixture generation is not a test dependency.
