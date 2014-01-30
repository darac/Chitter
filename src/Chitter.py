#!/usr/bin/env python3

"""
    Chitter:
    An XMPP bot to forward selected twitter notifications
    (mentions, replies, DMs and posts from selected friends)
    to a JID
"""

# built-ins
import argparse
from datetime import datetime, timedelta
import logging
import sqlite3
from threading import Thread
import unicodedata

from sleekxmpp import ClientXMPP
from sleekxmpp.exceptions import XMPPError#, IqError, IqTimeout

from twython import Twython, TwythonStreamer, TwythonError

import potr

# Locals
from Singleton import Singleton
from ttp import ttp
import IndentFormatter
from src.XMPPOTR import pOTRContext
from src.XMPPOTR.OTRContextManager import PyOTRContextManager


class ChitterBuffer(metaclass=Singleton):
    def __init__(self):
        self.con = sqlite3.connect(':memory:', check_same_thread = False)
        self.con.row_factory = sqlite3.Row
        self.cur = self.con.cursor()
        self.cur.execute('''CREATE TABLE tweets
                         (id INTEGER PRIMARY KEY,
                          id_str TEXT,
                          user_id TEXT,
                          content TEXT,
                          for TEXT,
                          is_deleted INTEGER,
                          is_dm INTEGER)''')

    def add(self, jid, tweet, is_dm=False):
        logging.debug("Adding {} for {}".format(is_dm and "DM" or "Tweet", jid))
        # First of all, do we already have this tweet?
        self.cur.execute('SELECT id, is_deleted FROM tweets WHERE id_str=?', (tweet['id_str'],))
        row = self.cur.fetchone()
        if row and not row['is_deleted']:
            return "%04x" % row['id']
        elif row:
            # Exists, but is deleted
            return ""

        # Tweet doesn't exist, so add it
        if 'user' in tweet:
            self.cur.execute ('INSERT INTO tweets (id_str, user_id, content, for, is_deleted, is_dm) VALUES (?,?,?,?,?,?)', (tweet['id_str'], tweet['user']['id_str'], tweet['text'], jid, False, is_dm))
        elif 'sender' in tweet:
            self.cur.execute ('INSERT INTO tweets (id_str, user_id, content, for, is_deleted, is_dm) VALUES (?,?,?,?,?,?)', (tweet['id_str'], tweet['sender']['id_str'], tweet['text'], jid, False, is_dm))
        self.con.commit()
        return "%04x" % self.cur.lastrowid

    def get(self, msgid, jid):
        self.cur.execute('SELECT id_str, user_id, content, is_dm FROM tweets WHERE is_deleted=? AND for=? AND id=?', (False, jid, int(msgid,16)))
        return self.cur.fetchone()

    def remove(self, jid, tweet):
        logging.debug("Removing tweet for {}".format(jid))
        # First of all, do we already have this tweet?
        if 'status' in tweet['delete']:
            id_str = tweet['delete']['status']['id_str']
            is_dm = False
        elif 'direct_message' in tweet['delete']:
            id_str = tweet['delete']['direct_message']['id_str']
            is_dm = True
        self.cur.execute('SELECT id, is_deleted FROM tweets WHERE id_str=?', (id_str,))
        row = self.cur.fetchone()
        if row and not row['is_deleted']:
            self.cur.execute('UPDATE tweets SET is_deleted=? WHERE id_str=?', (True, id_str))
        elif row:
            # Exists and is already deleted
            pass
        else:
            self.cur.execute('INSERT INTO tweets(id_str,is_deleted,is_dm) VALUES (?,?,?)', (id_str, True, is_dm))
        self.con.commit()

    def purge(self, jid):
        logging.debug("Puring cached tweets for {}".format(jid))
        self.cur.execute('DELETE FROM tweets WHERE for=?', (jid,))
        self.cur.execute('VACUUM')
        self.con.commit()
        


class ChitterThread(Thread):
    def __init__(self, barejid, app_key, app_secret, oauth_token, oauth_token_secret, kind):
        super().__init__()
        self.stream = ChitterStream(barejid, app_key, app_secret, oauth_token, oauth_token_secret, kind=kind)
        self.kind = kind

    def run(self):
        if self.kind == 'mentions':
            user_info = self.stream.twitter.verify_credentials()
            self.stream.statuses.filter(track='@%s' % (user_info['screen_name'],))
        if self.kind == 'dms':
            self.stream.user()


class ChitterStream(TwythonStreamer):
    # Streaming API listener
    
    def __init__(self, barejid, app_key, app_secret, oauth_token, oauth_token_secret, kind, timeout=300, retry_count=None, retry_in=10, client_args=None, handlers=None, chunk_size=1):
        super().__init__(app_key, app_secret, oauth_token, oauth_token_secret, timeout, retry_count, retry_in, client_args, handlers, chunk_size)
        self.buff = ChitterBuffer()
        self.xmpp = ChitterBot()
        self.jid = barejid
        self.kind = kind
        logging.debug("Starting ChitterStream for %s", self.jid)

        # We also want a Twython (REST API) object for doing REST
        self.twitter = Twython(app_key, app_secret, oauth_token, oauth_token_secret)

    def on_success(self,data):
        # Got a message from the stream!
        # (send it to the user)
        if 'delete' in data:
            self.buff.remove(self.jid, data)
        if self.kind == 'dms' and 'direct_message' not in data:
            # Want DMs, but this isn't a DM
            logging.debug("Dropping Message (not a DM)")
            return
        elif self.kind == 'dms':
            msgid = self.buff.add(self.jid, data['direct_message'], is_dm=True)
            if msgid != "":
                self.xmpp.send_message(
                        mto=self.jid,
                        mbody="%(msgid)s> [DIRECT] %(user_desc)s (@%(screenname)s): %(tweet)s" %
                            {'msgid': msgid,
                             'user_desc': data['direct_message']['sender']['name'],
                             'screenname': data['direct_message']['sender']['screen_name'],
                             'tweet': data['direct_message']['text']},
                        mtype='normal')
        if 'text' in data:
            msgid = self.buff.add(self.jid, data)
            if msgid != "":
                self.xmpp.send_message(
                        mto=self.jid,
                        mbody="%(msgid)s> %(user_desc)s (@%(screenname)s): %(tweet)s" %
                            {'msgid': msgid,
                             'user_desc': data['user']['name'],
                             'screenname': data['user']['screen_name'],
                             'tweet': data['text']},
                        mtype='normal')
        

    def on_error(self, status_code, data):
        try:
            data = data.decode('utf-8')
        except:
            pass
        logging.error(data)
        self.xmpp.send_message(
                mto=self.jid,
                mbody="Whoops! %d: %s" % (status_code, data),
                mtype='normal')

class ChitterBot(ClientXMPP, metaclass=Singleton):
    """
        Chitter:
        An XMPP bot to forward selected twitter notifications
        (mentions, replies, DMs and posts from selected friends)
        to a JID
    """

    def __init__(self, jid, password):
        ClientXMPP.__init__(self, jid, password)
        
        # The session_start event will be triggered when
        # the bot establishes its connection with the server
        # and the XML streams are ready for use. We want to
        # listen for this event so that we we can intialize
        # our roster.
        self.add_event_handler("session_start", self.session_start)

        # The message event is triggered whenever a message
        # stanza is received. Be aware that that includes
        # MUC messages and error messages.
        self.add_event_handler("message", self.message)

        # We want to handle subscription ourselves (mainly, so we can ask
        # the user to authorise us)
        #self.auto_authorize = None
        self.add_event_handler('presence_subscribe', self.subscribe)
        self.add_event_handler('presence_subscribed', self.subscribed)
        self.add_event_handler('presence_unsubscribe', self.unsubscribe)
        self.add_event_handler('presence_unsubscribed', self.unsubscribed)
        self.add_event_handler('presence_available', self.user_online)
        self.add_event_handler('got_online', self.user_online)
        self.add_event_handler('presence_unavailable', self.user_offline)
        self.add_event_handler('got_offline', self.user_offline)

        # Twitter config
        self.TW_APP_KEY = 'uO8yTJK7coIRZXl9yd0g'
        self.TW_APP_SECRET = '3Gw5Jk8CwqwX4jPTkPTh0nBMynQXLcqXAeDMX0tEXc'

        # Database connection
        conn = sqlite3.connect('/var/local/chitter.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (jid TEXT PRIMARY KEY,
                      oauth_token TEXT,
                      oauth_token_secret TEXT,
                      auth_state TEXT DEFAULT "init")''')
        c.execute('''CREATE TABLE IF NOT EXISTS options
                     (jid TEXT PRIMARY KEY REFERENCES users(jid) ON DELETE CASCADE,
                      want_mentions INTEGER DEFAULT 1,
                      want_dms INTEGER DEFAULT 1,
                      want_stalks INTEGER DEFAULT 0
                      )''')
        conn.commit()
        c.close()

        self.streams = {}
        self.composed = {}


    def session_start(self, event):
        self.send_presence(pstatus="Starting...", pshow='dnd')

        try:
            self.get_roster()
        except XMPPError as err:
        #except IqError as err:
            logging.error('There was an error getting the roster')
            logging.error(err.iq['error']['condition'])
            self.disconnect()
        #except IqTimeout:
        #    logging.error('Server is taking too long to respond')
        #    self.disconnect()

        self.otr_manager = PyOTRContextManager(self.boundjid.bare)

        # For each user, start a ChitterStream
        conn =  sqlite3.connect('/var/local/chitter.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT jid, oauth_token, oauth_token_secret FROM users WHERE auth_state=?", ('authorized',))
        rows = cursor.fetchall()
        for row in rows:
            if self.is_user_online(row['jid']):
                self.StartChitterThreads(row['jid'])

        self.send_presence(pstatus="Ready. Send '/help' for help", pshow='chat')
        conn.close()

    def subscribe(self, presence):
        logging.info('New User %s requests subscription. Sending subscription back', presence['from'].bare)
        # Request bi-directional subscription
        if presence['from'].bare not in self.roster:
            self.sendPresence(pto=presence['from'],
                              ptype='subscribe')

    def subscribed(self, presence):
        if presence['from'].bare == self.boundjid.bare:
            # derp!
            logging.info('Not sending messages to myself')
            return
        logging.info('New User %s authorised us. Start Twitter Authentication', presence['from'].bare)
        # Send presence update to the subscriber
        self.sendPresence(pto=presence['from'])

        # Now ask the user to authorise us on twitter
        # Note, if we already know the user, this will cause
        # re-authentication, but for a subscription, we shouldn't
        # already know them
        self.send_message(mto=presence['from'],
                          mbody="Welcome to Chitter!",
                          mtype='normal')
        self.StartAuthentication(presence['from'])

    def unsubscribe(self, presence):
        logging.info('User %s requested unsubsciption. Sending unsubscription, back', presence['from'].bare)
        # Request bi-directional unsubscription
        if presence['from'].bare in self.roster:
            self.sendPresence(pto=presence['from'],
                              ptype='unsubscribe')

    def unsubscribed(self, presence):
        logging.info('User %s de-authorised us. Forgetting user', presence['from'].bare)
        # Send presence update to the unsubscriber
        self.sendPresence(pto=presence['from'])

        # Remove the user from the database

    def decrypt_message(self, msg):
        otrctx = self.otr_manager.get_context_for_user(str(msg['from']))
        encrypted = True
        try:
            res = otrctx.receiveMessage(msg['body'])
        except potr.context.UnencryptedMessage as message:
            # Recieved an unencrypted message in an encrypted context
            # This is a problem
            self.send_message(mto=msg['from'], mtype=msg['type'], mbody="WARNING! Plaintext message recieved during OTR session :(")
            encrypted = False

        if encrypted and res[0] is not None:
            msg['body'] = res[0]

        # Pass the decrypted message onwards
        self.message(msg)

    def encrypt_message(self,msg):
        otrctx = self.otr_manager.get_context_for_user(str(msg['to']))
        if otrctx.state == potr.context.STATE_ENCRYPTED:
            logging.debug("Encrypting message")
            otrctx.sendMessage(0, msg)
        else:
            logging.debug("Sending message unencrypted")
            msg.send()

    def message(self, msg):
        if msg['from'].bare == self.boundjid.bare:
            # derp!
            logging.info('Not sending messages to myself')
            return

        conn =  sqlite3.connect('/var/local/chitter.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if msg['type'] in ('normal', 'chat'):
            otrctx = self.otr_manager.get_context_for_user(str(msg['from']))

            logging.debug("Message from %(from)s: %(body)s" % msg)

            # What state is the user's authentication in?
            cursor.execute ("SELECT auth_state FROM users WHERE jid=?", (msg['from'].bare,))
            row = cursor.fetchone()
            if row is None:
                logging.warning("Ack! Don't know the user %s", msg['from'].bare)
            else:
                auth_state = row['auth_state']

            if auth_state == 'init':
                # Whoa, new user
                self.StartAuthentication(msg['from'])
            elif auth_state == 'pending':
                # Got the pin
                self.CompleteAuthentication(msg['from'], msg['body'])
            elif auth_state == 'authorized':
                # Normal mode
                if msg['body'].startswith('/'):
                    if msg['body'].lower().strip() == '/help':
                        cursor.execute("SELECT want_mentions, want_dms FROM options WHERE jid=?", (msg['from'].bare,))
                        row = cursor.fetchone()
                        if row['want_mentions']:
                            mentions = 'ON'
                        else:
                            mentions = 'OFF'
                        if row['want_dms']:
                            dms = 'ON'
                        else:
                            dms = 'OFF'
                        msg.reply("""Chitter: A Twitter to XMPP bot.

Commands:
/help            Show this message.
/want_mentions   Toggle (non-reply) mentions of you. Currently: %(mentions)s.
/want_DMs        Toggle Direct Messages: Currently: %(dms)s.
/reply {code} {message}
                 Reply to a specific tweet.
/tweet {message} Send a tweet.
/tweet           Send a composed tweet (within 5 minutes).
/dm {user} {message}
                 Send a direct message.
/dm {user}       Send a composed direct message.
/delete {code}   Delete a tweet.         
{message}        Compose a tweet. I'll tell you how many characters it is.
""" % {'mentions': mentions, 'dms': dms}).send()
                    if msg['body'].lower().strip() in ('/want_mentions', '/want_dms'):
                        cursor.execute("SELECT want_mentions, want_dms FROM options WHERE jid=?", msg['from'].bare)
                        row = cursor.fetchone()
                        if msg['body'].lower() == '/want_mentions':
                            row['want_mentions'] = not row['want_mentions']
                            if row['want_mentions']:
                                msg.reply("Mentions are now ON").send()
                            else:
                                msg.reply("Mentions are now OFF").send()
                        if msg['body'].lower() == '/want_dms':
                            row['want_dms'] = not row['want_dms']
                            if row['want_dms']:
                                msg.reply("DMs are now ON").send()
                            else:
                                msg.reply("DMs are now OFF").send()
                        cursor.execute("UPDATE options SET want_mentions=?, want_dms=? WHERE jid=?", (row['want_mentions'], row['want_dms'], msg['from'].bare))
                        self.StartChitterThreads(msg['from'].bare, restart=True)

                    if msg['body'].lower().strip().startswith('/delete'):
                        cmd_parts = msg['body'].lower().split(maxsplit=1)
                        tweet = ChitterBuffer().get(cmd_parts[1], msg['from'].bare)
                        if tweet is None:
                            msg.reply("Sorry, I don't know message ID %s. (Maybe it's been deleted?)" % cmd_parts[1]).send()
                        elif tweet['is_dm']:
                            self.streams[msg['from'].bare]['dms'].stream.twitter.destroy_direct_message(id=tweet['id_str'])
                        else:
                            self.streams[msg['from'].bare]['dms'].stream.twitter.destroy_status(id=tweet['id_str'])
                        msg.reply("OK. Item deleted").send()



                    if msg['body'].lower().strip().startswith('/reply'):
                        cmd_parts = msg['body'].lower().split(maxsplit=2)
                        tweet = ChitterBuffer().get(cmd_parts[1], msg['from'].bare)
                        if tweet is None:
                            msg.reply("Sorry, I don't know message ID %s. (Maybe it's been deleted?)" % cmd_parts[1]).send()
                        else:
                            if 2 > len(cmd_parts)-1 and self.composed[msg['from'].bare] != "":
                                # No reply body, but there is a composed
                                # Use that
                                reply_body = self.composed[msg['from'].bare]
                            elif 2 <= len(cmd_parts)-1:
                                # There is a reply body
                                reply_body = cmd_parts[2]
                            else:
                                self.send_message(mto=msg['from'], mtype=msg['type'],mbody="No reply body and nothing composed (has it expired?). Not sending.")
                                return

                            try:
                                if tweet['is_dm']:
                                    msgid = ChitterBuffer().add(msg['from'].bare,
                                            self.streams[msg['from'].bare]['dms'].stream.twitter.send_direct_message(user_id=tweet['user_id'], text=reply_body),
                                            is_dm=True)

                                else:
                                    # If the user being replied to isn't mentioned
                                    # in the tweet, then we need to add it.
                                    # Convention prepends the name to the tweet
                                    reply_user = self.streams[msg['from'].bare]['dms'].stream.twitter.lookup_user(user_id=tweet['user_id'])[0]
                                    logging.debug("Checking that reply mentions @{}".format(reply_user))
                                    if "@{}".format(reply_user['screen_name']).lower() not in str(reply_body).lower():
                                        reply_body = "@%s %s" % (reply_user['screen_name'], reply_body)

                                    # Post the reply and clear (any) composed message
                                    msgid = ChitterBuffer().add(msg['from'].bare,
                                            self.streams[msg['from'].bare]['dms'].stream.twitter.update_status(status=reply_body),
                                            is_dm=False)
                            except TwythonError as e:
                                logging.warning(e)
                                self.send_message(mto=msg['from'], mtype=msg['type'], mbody=e)
                            else:
                                self.composed[msg['from'].bare] = ""
                                newtweet = ChitterBuffer().get(msgid, msg['from'].bare)
                                self.send_message(
                                        mto=msg['from'],
                                        mbody="%(msgid)s> [SENT]: %(tweet)s" %
                                            {'msgid': msgid,
                                             'tweet': newtweet['content']},
                                        mtype=msg['type'])


                    if msg['body'].lower().strip().startswith('/tweet'):
                        cmd_parts = msg['body'].lower().split(maxsplit=1)
                        if 1 > len(cmd_parts)-1 and self.composed[msg['from'].bare] != "":
                            # No tweet body, but there is a composed
                            # Use that
                            tweet_body = self.composed[msg['from'].bare]
                        elif 1 <= len(cmd_parts)-1:
                            # There is a reply body
                            tweet_body = cmd_parts[2]
                        else:
                            self.send_message(mto=msg['from'], mtype=msg['type'],mbody="No tweet body and nothing composed (has it expired?). Not sending.")
                            return

                        try:
                            # Post the tweet and clear (any) composed message
                            msgid = ChitterBuffer().add(msg['from'].bare,
                                    self.streams[msg['from'].bare]['dms'].stream.twitter.update_status(status=tweet_body),
                                    is_dm=False)
                            newtweet = ChitterBuffer().get(msgid, msg['from'].bare)
                            self.composed[msg['from'].bare] = ""
                            self.send_message(
                                    mto=msg['from'],
                                    mbody="%(msgid)s> [SENT]: %(tweet)s" %
                                        {'msgid': msgid,
                                         'tweet': newtweet['content']},
                                    mtype=msg['type'])
                        except TwythonError as e:
                            logging.warning(e)
                            self.send_message(mto=msg['from'], mtype=msg['type'], mbody=e)

                    if msg['body'].lower().strip().startswith('/dm'):
                        cmd_parts = msg['body'].lower().split(maxsplit=2)
                        if 1 > len(cmd_parts)-1:
                            # No options
                            self.send_message(mto=msg['from'], mtype=msg['type'], mbody="You must specify who you want to send the DM to.")
                            return
                        elif 2 > len(cmd_parts)-1 and self.composed[msg['from'].bare] != "":
                            # No tweet body, but there is a composed
                            # Use that
                            tweet_body = self.composed[msg['from'].bare]
                        elif 2 <= len(cmd_parts)-1:
                            # There is a reply body
                            tweet_body = cmd_parts[2]
                        else:
                            self.send_message(mto=msg['from'], mtype=msg['type'],mbody="No tweet body and nothing composed (has it expired?). Not sending.")
                            return

                        if cmd_parts[1].startswith('@'):
                            tweet_target=cmd_parts[1].lstrip('@')
                        else:
                            tweet_target=cmd_parts[1]

                        try:
                            # Post the tweet and clear (any) composed message
                            msgid = ChitterBuffer().add(msg['from'].bare,
                                    self.streams[msg['from'].bare]['dms'].stream.twitter.send_direct_message(screen_name=tweet_target, text=tweet_body),
                                    is_dm=True)
                            newtweet = ChitterBuffer().get(msgid, msg['from'].bare)
                            self.composed[msg['from'].bare] = ""
                            self.send_message(
                                    mto=msg['from'],
                                    mbody="%(msgid)s> [SENT TO @%(target)s]: %(tweet)s" %
                                        {'msgid': msgid,
                                         'target': tweet_target,
                                         'tweet': newtweet['content']},
                                    mtype=msg['type'])
                        except TwythonError as e:
                            logging.warning(e)
                            self.send_message(mto=msg['from'], mtype=msg['type'], mbody=e)


                else:
                    # Compose a tweet
                    self.composed[msg['from'].bare] = msg['body']

                    # Count the characters
                    normalised = unicodedata.normalize('NFC',msg['body'])
                    charcount = len(normalised)

                    # Find URLs
                    p = ttp.Parser()
                    parsed = p.parse(normalised)
                    for url in parsed.urls:
                        if url.startswith('https'):
                            charcount=charcount-len(url)+23
                        else:
                            charcount=charcount-len(url)+22
                    if charcount > 140:
                        warn = "Twitter will probably reject this"
                    else:
                        warn = "Twitter should be happy with this"

                    self.send_message(mto=msg['from'], mtype=msg['type'], mbody="OK. I make your message to be %d characters. %s." % (charcount, warn))
                    self.schedule('clear_%s'%msg['from'].bare, 600, self.ClearComposed, args=(msg['from'].bare,))
                    self.send_message(mto=msg['from'], mtype=msg['type'], mbody="I will remember that tweet until {:%H:%M:%S}. Use /tweet, /reply {{code}} or /dm {{user}} to send it.".format(datetime.now() + timedelta(minutes=5)))






        conn.close()

    def ClearComposed(self, jid):
        logging.debug("Expiring composed tweet for %s", jid)
        self.composed[jid] = ""

    def StartAuthentication(self, jid):
        if jid.bare == self.boundjid.bare:
            # derp!
            logging.info('Not sending messages to myself')
            return
        twitter = Twython(self.TW_APP_KEY, self.TW_APP_SECRET)
        # Temporary tokens
        auth = twitter.get_authentication_tokens()

        conn =  sqlite3.connect('/var/local/chitter.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO users VALUES (?,?,?,?)',
                            (jid.bare,
                             auth['oauth_token'],
                             auth['oauth_token_secret'],
                             'pending'))
        cursor.execute('INSERT OR REPLACE INTO options VALUES (?,?,?,?)',
                jid.bare, True, True, False)
        conn.commit()
        conn.close()

        self.send_message(mto=jid,
                          mbody="Please visit %(auth_url)s in a browser to authenticate, then reply with the PIN you're given" % {'auth_url': auth['auth_url']},
                          mtype='normal')

    def CompleteAuthentication(self, jid, pin):
        conn =  sqlite3.connect('/var/local/chitter.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT oauth_token, oauth_token_secret FROM users WHERE jid=?", (jid.bare,))
        (oauth_token, oauth_token_secret) = cursor.fetchone()

        twitter = Twython(self.TW_APP_KEY, self.TW_APP_SECRET, oauth_token, oauth_token_secret)
        auth = twitter.get_authorized_tokens(pin)

        # Final tokens
        oauth_token = auth['oauth_token']
        oauth_token_secret = auth['oauth_token_secret']

        cursor.execute('UPDATE users SET oauth_token=?, oauth_token_secret=?, auth_state=?', (oauth_token, oauth_token_secret, 'authorized'))

        self.send_message(mto=jid,
                          mbody="Welcome %(screen_name)s! You're now authorised." % auth,
                          mtype='normal')
        conn.commit()
        conn.close()

    def is_user_online(self, barejid):
        IsOnline = False
        for presence in self.roster[barejid]['presence']:
            logging.debug("Checking %s presence '%s': %s" % (barejid, presence, self.roster[barejid]['presence'][presence]['show']))
            IsOnline = IsOnline or (self.roster[barejid]['presence'][presence]['show'] != 'offline')
        return IsOnline

    def user_online(self, presence):
        if presence['from'].bare == self.boundjid.bare:
            # derp!
            logging.info('Not sending messages to myself')
            return
        # Connect the Chitter Streams
        conn = sqlite3.connect('/var/local/chitter.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT oauth_token, oauth_token_secret FROM users WHERE auth_state=? AND jid=?", ('authorized',presence['from'].bare))
        row = cursor.fetchone()
        if row is None:
            logging.info ("Unauthorized User %s Came online", presence['from'].bare)
            self.send_message(mto=presence['from'],
                              mbody="Welcome to Chitter!",
                              mtype='normal')
            self.StartAuthentication(presence['from'])
        elif self.is_user_online(presence['from'].bare):
            self.StartChitterThreads(presence['from'].bare)
        conn.close()

    def user_offline(self, presence):
        # The user might be logged in multiply,
        # Only purge if there is no entry for them left online
        if not self.is_user_online(presence['from'].bare):
            # User is (fully) offline
            # Disconnect the Chitter Streams
            if presence['from'].bare in self.streams:
                logging.info("User %s going offline" % presence['from'].bare)
                ChitterBuffer().purge(presence['from'].bare)
                for p in self.streams[presence['from'].bare]:
                    self.streams[presence['from'].bare][p].stream.disconnect()
                del self.streams[presence['from'].bare]

    def StartChitterThreads(self, jid, restart=False):
        if jid not in self.streams or restart:
            if restart:
                logging.info("Disconnecting stale ChitterStreams for %s", jid)
                for stream in self.streams:
                    self.streams[jid][stream].stream.disconnect()
            logging.info("Creating ChitterStreams for %s", jid)
            self.streams[jid] = {}
            conn = sqlite3.connect('/var/local/chitter.db')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT oauth_token, oauth_token_secret FROM users WHERE auth_state=? AND jid=?", ('authorized',jid))
            row = cursor.fetchone()
            if row is None:
                logging.error("Can't start Chitter Threads for unauthorized user %s", jid)
                return
            cursor.execute("SELECT want_mentions, want_dms FROM options WHERE jid=?", (jid,))
            options = cursor.fetchone()
            if options['want_mentions']:
                self.streams[jid]['mentions'] = ChitterThread(
                        barejid = jid,
                        app_key = self.TW_APP_KEY,
                        app_secret = self.TW_APP_SECRET,
                        oauth_token = row['oauth_token'],
                        oauth_token_secret = row['oauth_token_secret'],
                        kind='mentions')
                self.streams[jid]['mentions'].start()
            if options['want_dms']:
                self.streams[jid]['dms'] = ChitterThread(
                        barejid = jid,
                        app_key = self.TW_APP_KEY,
                        app_secret = self.TW_APP_SECRET,
                        oauth_token = row['oauth_token'],
                        oauth_token_secret = row['oauth_token_secret'],
                        kind='dms')
                self.streams[jid]['dms'].start()
            conn.close()
        

if __name__ == '__main__':
    
    try:
        argparser = argparse.ArgumentParser(description="Twitter to XMPP bot")
        argparser.add_argument('-v', '--verbose', help='log at DEBUG level',
                action='store_true')
        argparser.add_argument('-d', '--daemonize', help='Run as a daemon',
                action='store_true')
        args = argparser.parse_args()
        if args.verbose:
            log_level = logging.DEBUG
        else:
            log_level = logging.INFO

        loggingformatter = IndentFormatter.IndentFormatter("[%(levelname)s]%(indent)s%(function)s:%(message)s")
        logger = logging.getLogger()
        handler = logging.StreamHandler()
        handler.setFormatter(loggingformatter)
        logger.addHandler(handler)
        logger.setLevel(log_level)

        xmpp = ChitterBot('chitter@darac.org.uk', 'Eil5quai8ohb')
        xmpp.connect()
        xmpp.process(threaded=False)

    finally:
        if args.daemonize:
            # Remove the PID file
            pass
