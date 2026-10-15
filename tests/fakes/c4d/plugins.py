REGISTERED = []

class CommandData(object):
    pass

def RegisterCommandPlugin(**k):
    REGISTERED.append(k)
    return True
