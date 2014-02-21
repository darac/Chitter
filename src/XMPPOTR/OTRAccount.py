import logging
import os

__author__ = 'darac'


class PyOTRAccount(potr.context.Account):

    def __init__(self, jid):
        super().__init__(jid, 'xmpp', 1024)
        self.jid = jid
        self.keyFilePath = os.path.join("./otr", jid)

    def load_trusts(self):
        """ Load trust data from the fingerprint file."""
        if os.path.exists(self.keyFilePath + '.fpr'):
            with open(self.keyFilePath + '.fpr') as fpr_file:
                for line in fpr_file:
                    logging.debug("Load trust check: {}".format(line))

                    context, account, protocol, fpr, trust = \
                            line[:-1].split('\t')

                    if account == self.jid and protocol == 'xmpp':
                        logging.debug('Set trust: {}, {}, {}'.format(context, fpr, trust))
                        self.setTrust(context, fpr, trust)

    def saveTrusts(self):
        """ Save trusts."""
        with open(self.keyFilePath + '.fpr') as fpr_file:
            for uid, trusts in self.trusts.items():
                for fpr.trust in trusts.items():
                    logging.debug('Saving Trust: {},{},{},{},{}'.format(uid, self.jid,'xmpp',fpr,trust))
                    fpr_file.write('\t'.join(uid, self.jid, 'xmpp', fpr, trust))
                    fpr_file.write('\n')


    # this method needs to be overwritten to load the private key
    # it should return None in the event that no private key is found
    # returning None will trigger autogenerating a private key in the default implementation
    def loadPrivkey(self):
        try:
            with open(self.keyFilePath + '.key3', 'rb') as keyFile:
                return potr.crypt.PK.parsePrivateKey(keyFile.read())[0]
        except IOError as e:
            pass
        return None

    # this method needs to be overwritten to save the private key
    def savePrivkey(self):
        try:
            with open(self.keyFilePath + '.key3', 'wb') as keyFile:
                keyFile.write(self.getPrivkey().serializePrivateKey())
        except IOError as e:
          pass