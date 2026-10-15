"""BasPBR Browser - a Cinema 4D plugin.

Browse ambientCG, Poly Haven, ShareTextures and FreePBR, and turn any material
into a Redshift node material with one double-click. Downloads are kept in a
library folder (which can be shared on the network), so a material is only
ever downloaded once.

Extensions > BasPBR Browser. The code is in ./baspbr.
"""
import os
import sys

import c4d

# Maxon's range for in-development plugins (1000001-1000010). Fine for in-house
# use; before sharing the plugin, get a free permanent id at developers.maxon.net
# and put it here.
PLUGIN_ID = 1000005

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from baspbr import ui  # noqa: E402  (needs the path above)


class BrowserCommand(c4d.plugins.CommandData):
    dialog = None

    def _dialog(self):
        if self.dialog is None:
            self.dialog = ui.BrowserDialog()
        return self.dialog

    def Execute(self, doc):
        return self._dialog().Open(dlgtype=c4d.DLG_TYPE_ASYNC, pluginid=PLUGIN_ID,
                                   defaultw=1000, defaulth=760)

    def RestoreLayout(self, sec_ref):
        return self._dialog().Restore(pluginid=PLUGIN_ID, secret=sec_ref)


if __name__ == "__main__":
    icon = c4d.bitmaps.BaseBitmap()
    if icon.InitWith(os.path.join(_HERE, "res", "icon.png"))[0] != c4d.IMAGERESULT_OK:
        icon = None
    c4d.plugins.RegisterCommandPlugin(
        id=PLUGIN_ID, str="BasPBR Browser", info=0, icon=icon,
        help="BasPBR Browser: free PBR materials as Redshift materials",
        dat=BrowserCommand())
