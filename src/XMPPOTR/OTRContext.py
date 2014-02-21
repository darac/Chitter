__author__ = 'darac'

import potr

class PyOTRContext(potr.context.Context):
    # https://blog.darmasoft.net/2013/06/30/using-pure-python-otr.html
    def __init__(self, account, peer):
        super().__init__(account, peer)
        self.DEFAULT_OTR_POLICY = {
            'ALLOW_V1' : False,
            'ALLOW_V2' : True,
            'REQUIRE_ENCRYPTION' : False,
        }

    def getPolicy(self, key):
        if key in self.DEFAULT_OTR_POLICY:
            return self.DEFAULT_OTR_POLICY[key]
        else:
            return False

    def inject(self, msg, appdata=None):
        log.debug('inject(%s, appdata=%s)' % (msg, appdata))
        # this method is called when potr needs to inject a message into
        # the stream.  for instance, upon receiving an initiating stanza,
        # potr will inject the key exchange messages here is where you
        # should hook into your app and actually send the message potr gives you
