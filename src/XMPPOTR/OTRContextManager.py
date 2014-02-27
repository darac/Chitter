from XMPPOTR.OTRAccount import PyOTRAccount
from XMPPOTR.OTRContext import PyOTRContext

__author__ = 'darac'


class PyOTRContextManager:
  # the jid parameter is the logged in user's jid.
  # I use it to instantiate an object of the *potr.context.Account*
  # subclass described earlier.
  def __init__(self, jid):
    self.account = PyOTRAccount(jid)
    self.contexts = {}

  # this method starts a context with a peer if none exists,
  # or returns it otherwise
  def start_context(self, other):
    if other not in self.contexts:
      self.contexts[other] = PyOTRContext(self.account, other)
    return self.contexts[other]

  # just an alias for start_context
  def get_context_for_user(self, other):
    return self.start_context(other)
