"""BasPBR Browser - a Cinema 4D plugin.

Browse ambientCG, Poly Haven, ShareTextures, AMD GPUOpen and FreePBR materials
and Sketchfab models, and bring them in with Redshift node materials in one
double-click. Downloads are kept in library folders (which can be shared on the
network), so nothing is ever downloaded twice.

Extensions > BasPBR Browser. The code is in ./baspbr.

If the code can't load (a Cinema 4D update changed something), the command still
registers: opening it says what went wrong, and the Console has the full error
plus the Cinema 4D / Python versions and the folder it was loaded from.
"""
import os
import sys
import traceback

import c4d

# Maxon's range for in-development plugins (1000001-1000010). Fine for in-house
# use; a permanent id from developers.maxon.net (free, with a Maxon account) is
# safer once several people use the plugin.
PLUGIN_ID = 1000005

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from baspbr import ui, version  # noqa: E402  (needs the path above)
    LOAD_ERROR = None
except Exception:                   # keep the menu entry; say what broke when it's opened
    ui = version = None
    LOAD_ERROR = traceback.format_exc()


def _where():
    try:
        c4d_version = str(c4d.GetC4DVersion())
    except Exception:
        c4d_version = "?"
    return "Cinema 4D %s, Python %s, from %s" % (c4d_version, sys.version.split()[0], _HERE)


class BrowserCommand(c4d.plugins.CommandData):
    dialog = None

    def _dialog(self):
        if self.dialog is None:
            self.dialog = ui.BrowserDialog()
        return self.dialog

    def Execute(self, doc):
        if LOAD_ERROR:
            c4d.gui.MessageDialog("BasPBR Browser couldn't start:\n\n%s\n\n(%s)\n\nThe full error is in the Console "
                                  "(Extensions > Console, Python)." % (LOAD_ERROR.strip().splitlines()[-1], _where()))
            return True
        return self._dialog().Open(dlgtype=c4d.DLG_TYPE_ASYNC, pluginid=PLUGIN_ID,
                                   defaultw=1000, defaulth=760)

    def RestoreLayout(self, sec_ref):
        if LOAD_ERROR:
            return False
        return self._dialog().Restore(pluginid=PLUGIN_ID, secret=sec_ref)


if __name__ == "__main__":
    if LOAD_ERROR:
        print("[BasPBR Browser] couldn't load (%s):\n%s" % (_where(), LOAD_ERROR))
    icon = c4d.bitmaps.BaseBitmap()
    try:
        if icon.InitWith(os.path.join(_HERE, "res", "icon.png"))[0] != c4d.IMAGERESULT_OK:
            icon = None
    except Exception:
        icon = None
    try:
        ok = c4d.plugins.RegisterCommandPlugin(
            id=PLUGIN_ID, str="BasPBR Browser", info=0, icon=icon,
            help="BasPBR Browser: free PBR materials and Sketchfab models with Redshift materials",
            dat=BrowserCommand())
    except Exception:
        ok = False
        print("[BasPBR Browser] RegisterCommandPlugin failed (%s):\n%s" % (_where(), traceback.format_exc()))
    if not ok:
        print("[BasPBR Browser] not registered: plugin id %d is taken (%s). Is a second copy of BasPBR Browser "
              "(or the old MaterialBrowserRS) installed in another plugins folder, or another plugin using "
              "Maxon's test ids 1000001-1000010?" % (PLUGIN_ID, _where()))
    elif not LOAD_ERROR:
        print("[BasPBR Browser] %s ready (%s)" % (version.VERSION, _where()))
