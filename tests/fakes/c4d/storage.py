import os, tempfile
PREFS = os.path.join(tempfile.gettempdir(), "mbrs_fake_prefs")
os.makedirs(PREFS, exist_ok=True)
SHOWN = []

def GeGetC4DPath(which): return PREFS
def LoadDialog(**k): return None
def ShowInFinder(path, open=False): SHOWN.append(path)
