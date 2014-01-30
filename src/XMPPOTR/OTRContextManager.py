from src.XMPPOTR.OTRAccount import pOTRAccount

__author__ = 'darac'


class pOTRContextManager:
  # the jid parameter is the logged in user's jid.
  # I use it to instantiate an object of the *potr.context.Account*
  # subclass described earlier.
  def __init__(self, jid):
    self.account = pOTRAccount(jid)
    self.contexts = {}

  # this method starts a context with a peer if none exists,
  # or returns it otherwise
  def start_context(self, other):
    if other not in self.contexts:
      self.contexts[other] = pOTRContext(self.account, other)
    return self.contexts[other]

  # just an alias for start_context
  def get_context_for_user(self, other):
    return self.start_context(other)