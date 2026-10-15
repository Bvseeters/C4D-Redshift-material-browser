"""Fake BaseBitmap that reads only what Cinema 4D reads: PNG and JPEG (not WebP)."""


def _kind(path):
    with open(path, "rb") as f:
        head = f.read(12)
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    return None


REJECTED = []


class BaseBitmap(object):
    def __init__(self):
        self.w = self.h = 0
        self.image = None

    def InitWith(self, path):
        if _kind(path) is None:
            REJECTED.append(path)
            return (0, False)                # IMAGERESULT_WRONGTYPE-ish
        from PIL import Image
        self.image = Image.open(path).convert("RGB")
        self.w, self.h = self.image.size
        return (1, False)

    def Init(self, w, h, depth=24):
        self.w, self.h = w, h
        return 1

    def ScaleIt(self, dst, intens, sample, nprop):
        dst.image = self.image.resize((dst.w, dst.h))

    def Save(self, path, fmt):
        self.image.save(path, "PNG")
        return 1

    def GetBw(self): return self.w
    def GetBh(self): return self.h
