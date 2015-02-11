__version__ = '2.0'
__url__ = 'https://github.com/lilydjwg/fetchtitle'

import re
import time
import struct
import socket
import ssl
import logging
import encodings.idna
from functools import partial
from collections import namedtuple

try:
  py3 = True
  from urllib.parse import urlsplit, urljoin
except ImportError:
  py3 = False
  from urlparse import urlsplit, urljoin  # py2
  chr = unichr

try:
  from html.parser import HTMLParser
except ImportError:    #  py2
  from HTMLParser import HTMLParser

try:
  from html.entities import entitydefs
except ImportError:
  from htmlentitydefs import entitydefs # py2

import tornado.ioloop
import tornado.iostream
from tornado.httpclient import AsyncHTTPClient

# try to import C parser then fallback in pure python parser.
try:
  from http_parser.parser import HttpParser
except ImportError:
  from http_parser.pyparser import HttpParser

UserAgent = 'FetchTitle/%s (%s)' % (__version__, __url__)
_cookie_re = re.compile(r'(?:,\s*|^)([^=\s]+=[^;\s]+)')

def get_charset_from_ctype(ctype):
  pos = ctype.find('charset=')
  if pos > 0:
    charset = ctype[pos+8:]
    if charset.lower() == 'gb2312':
      # Windows misleadingly uses gb2312 when it's gbk or gb18030
      charset = 'gb18030'
    elif charset.lower() == 'windows-31j':
      # cp932's IANA name (Windows-31J), extended shift_jis
      # https://en.wikipedia.org/wiki/Code_page_932
      charset = 'cp932'
    try:
      ''.encode(charset)
      return charset
    except LookupError:
      logger.warn('got unknown character set name %r, ignoring.', charset)
      return

_context = None
def get_ssl_context():
  global _context
  if not _context:
    _context = ssl.create_default_context()
    # have to when not verifying certificates
    _context.check_hostname = False
    # don't verify certificates; there are too many untrusted sites, and we
    # won't get hurt by them
    _context.verify_mode = ssl.CERT_NONE
  return _context

def strip_and_collapse_whitespace(s):
  # http://www.w3.org/TR/html5/infrastructure.html#strip-and-collapse-whitespace
  return re.sub('[ \t\n\r\x0c]+', ' ', s).strip(' \t\n\r\x0c')

class HtmlTitleParser(HTMLParser):
  charset = title = None
  default_charset = 'utf-8'
  result = None
  _title_coming = False

  def __init__(self):
    # use a list to store literal bytes and escaped Unicode
    if py3:
        super().__init__()
    else:
        HTMLParser.__init__(self)
    self.title = []

  def feed(self, bytesdata):
    if bytesdata:
      data = bytesdata.decode('latin1')
      if py3:
        super().feed(data)
      else:
        HTMLParser.feed(self, data)
    else:
      self.close()

  def close(self):
    self._check_result(force=True)
    if py3:
      super().close()
    else:
      HTMLParser.close(self)

  def handle_starttag(self, tag, attrs):
    # Google Search uses wrong meta info
    # Baidu Cache declared charset twice. The former is correct.
    if tag == 'meta' and not self.charset:
      attrs = dict(attrs)
      # try charset attribute first. Wrong quoting may result in this:
      # <META http-equiv=Content-Type content=text/html; charset=gb2312>
      if attrs.get('charset', False):
        self.charset = attrs['charset']
      elif attrs.get('http-equiv', '').lower() == 'content-type':
        self.charset = get_charset_from_ctype(attrs.get('content', ''))
    elif tag == 'title':
      self._title_coming = True

    if not self._title_coming:
      self._check_result()

  def handle_data(self, data, # *, commented for Python 2
                  unicode=False):
    if not unicode and py3:
      data = data.encode('latin1') # encode back
    if self._title_coming:
      self.title.append(data)

  def handle_endtag(self, tag):
    if tag == 'title':
      self._title_coming = False
      self._check_result()

  def handle_charref(self, name):
    if name[0] == 'x':
      x = int(name[1:], 16)
    else:
      x = int(name)
    ch = chr(x)
    self.handle_data(ch, unicode=True)

  def handle_entityref(self, name):
    try:
      ch = entitydefs[name]
    except KeyError:
      ch = '&' + name
    self.handle_data(ch, unicode=True)

  def _check_result(self, # *, commented for Python 2
                    force=False):
    if self.result is not None:
      return

    if (force or self.charset is not None) \
       and self.title:
      string_type = str if py3 else unicode
      # always use 'replace' because surrogateescape may not be used elsewhere
      error_handler = 'replace'
      self.result = strip_and_collapse_whitespace(string_type().join(
        x if isinstance(x, string_type) else x.decode(
          self.charset or self.default_charset,
          errors = error_handler,
        ) for x in self.title
      ))

class SingletonFactory:
  def __init__(self, name):
    self.name = name
  def __repr__(self):
    return '<%s>' % self.name

MediaType = namedtuple('MediaType', 'type size dimension')
defaultMediaType = MediaType('application/octet-stream', None, None)

ConnectionClosed = SingletonFactory('ConnectionClosed')
TooManyRedirection = SingletonFactory('TooManyRedirection')
Timeout = SingletonFactory('Timeout')
TitleTooFaraway = SingletonFactory('TitleTooFaraway')

logger = logging.getLogger('fetchtitle')

class ContentFinder:
  buf = b''
  def __init__(self, mediatype):
    self._mt = mediatype

  @classmethod
  def match_type(cls, mediatype):
    ctype = mediatype.type.split(';', 1)[0]
    if hasattr(cls, '_mime') and cls._mime == ctype:
      return cls(mediatype)
    if hasattr(cls, '_match_type') and cls._match_type(ctype):
      return cls(mediatype)
    return False

class TitleFinder(ContentFinder):
  parser = None
  pos = 0
  maxpos = 1024 * 1024  # look at most around 1M as the title may be too long

  @staticmethod
  def _match_type(ctype):
    return ctype.find('html') != -1

  def __init__(self, mediatype):
    charset = get_charset_from_ctype(mediatype.type)
    self.parser = HtmlTitleParser()
    self.parser.charset = charset

  def __call__(self, data):
    if data:
      self.pos += len(data)
    exceeded = self.pos - self.maxpos
    if exceeded > 0:
      if exceeded < len(data):
        data = data[:-exceeded]
      else:
        data = b''
    self.parser.feed(data)
    if self.parser.result:
      return self.parser.result
    elif exceeded > 0:
      logger.warn('searched %d bytes but did not find title', self.maxpos)
      return TitleTooFaraway

class PNGFinder(ContentFinder):
  _mime = 'image/png'
  def __call__(self, data):
    if data is None:
      return self._mt

    self.buf += data
    if len(self.buf) < 24:
      # can't decide yet
      return
    if self.buf[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
      logging.warn('Bad PNG signature and header: %r', self.buf[:16])
      return self._mt._replace(dimension='Bad PNG')
    else:
      s = struct.unpack('!II', self.buf[16:24])
      return self._mt._replace(dimension=s)

class JPEGFinder(ContentFinder):
  _mime = 'image/jpeg'
  isfirst = True
  def __call__(self, data):
    if data is None:
      return self._mt

    # http://www.64lines.com/jpeg-width-height
    if data:
      self.buf += data

    if self.isfirst is True:
      # finding header
      if len(self.buf) < 5:
        return
      if self.buf[:3] != b'\xff\xd8\xff':
        logging.warn('Bad JPEG signature: %r', self.buf[:3])
        return self._mt._replace(dimension='Bad JPEG')
      else:
        if py3:
          self.blocklen = self.buf[4] * 256 + self.buf[5] + 2
        else:
          self.blocklen = ord(self.buf[4]) * 256 + ord(self.buf[5]) + 2
        self.buf = self.buf[2:]
        self.isfirst = False

    if self.isfirst is False:
      # receiving a block. 4 is for next block size
      if len(self.buf) < self.blocklen + 4:
        return
      buf = self.buf
      if buf[0] != 0xff:
        logging.warn('Bad JPEG: %r', self.buf[:self.blocklen])
        return self._mt._replace(dimension='Bad JPEG')
      if buf[1] == 0xc0 or buf[1] == 0xc2:
        s = buf[7] * 256 + buf[8], buf[5] * 256 + buf[6]
        return self._mt._replace(dimension=s)
      else:
        # not Start Of Frame, retry with next block
        self.buf = buf = buf[self.blocklen:]
        self.blocklen = buf[2] * 256 + buf[3] + 2
        return self(b'')

class GIFFinder(ContentFinder):
  _mime = 'image/gif'
  def __call__(self, data):
    if data is None:
      return self._mt

    self.buf += data
    if len(self.buf) < 10:
      # can't decide yet
      return
    if self.buf[:3] != b'GIF':
      logging.warn('Bad GIF signature: %r', self.buf[:3])
      return self._mt._replace(dimension='Bad GIF')
    else:
      s = struct.unpack('<HH', self.buf[6:10])
      return self._mt._replace(dimension=s)

class TitleFetcher:
  status_code = 0
  followed_times = 0 # 301, 302
  finder = None
  addr = None
  stream = None
  max_follows = 10
  timeout = 15
  _finished = False
  _cookie = None
  _connected = False
  _redirected_stream = None
  _content_finders = (TitleFinder, PNGFinder, JPEGFinder, GIFFinder)
  _url_finders = ()

  def __init__(self, url, callback, # *, commented for Python 2
               timeout=None, max_follows=None, io_loop=None,
               content_finders=None, url_finders=None, referrer=None,
               run_at_init=True):
    '''
    url: the (full) url to fetch
    callback: called with title or MediaType or an instance of SingletonFactory
    timeout: total time including redirection before giving up
    max_follows: max redirections

    may raise:
    <UnicodeError: label empty or too long> in host preparation
    '''
    self._callback = callback
    self.referrer = referrer
    if max_follows is not None:
      self.max_follows = max_follows

    if timeout is not None:
      self.timeout = timeout
    if hasattr(tornado.ioloop, 'current'):
        default_io_loop = tornado.ioloop.IOLoop.current
    else:
        default_io_loop = tornado.ioloop.IOLoop.instance
    self.io_loop = io_loop or default_io_loop()

    if content_finders is not None:
      self._content_finders = content_finders
    if url_finders is not None:
      self._url_finders = url_finders

    self.origurl = url
    self.url_visited = []
    self._run_at_init = run_at_init
    if run_at_init:
      self.run()

  def clone(self, *args, **kwargs):
    mykwargs = {
      'timeout': self.timeout,
      'max_follows': self.max_follows,
      'io_loop': self.io_loop,
      'content_finders': self._content_finders,
      'url_finders': self._url_finders,
      'referrer': self.fullurl,
      'run_at_init': self._run_at_init,
    }
    mykwargs.update(kwargs)
    return self.__class__(*args, **mykwargs)

  def run(self):
    if self.url_visited:
      raise Exception("can't run again")
    else:
      self.start_time = self.io_loop.time()
      self._timeout = self.io_loop.add_timeout(
        self.timeout + self.start_time,
        self.on_timeout,
      )
      try:
        self.new_url(self.origurl)
      except:
        self.io_loop.remove_timeout(self._timeout)
        raise

  def on_timeout(self):
    logger.debug('%s: request timed out', self.origurl)
    self.run_callback(Timeout)

  def parse_url(self, url):
    '''parse `url`, set self.host and self._protocol and return address'''
    self.url = u = urlsplit(url)
    self.host = u.netloc

    self._protocol = u.scheme
    if u.scheme == 'http':
      default_port = 80
    elif u.scheme == 'https':
      default_port = 443
    else:
      raise ValueError('bad url: %r' % url)

    addr = u.hostname, u.port or default_port
    return addr

  def new_connection(self, addr):
    '''set self.addr, self.stream and connect to host'''
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self.addr = addr

    if self._protocol == 'https':
      self.stream = tornado.iostream.SSLIOStream(
          s, ssl_options = get_ssl_context())
    else:
      self.stream = tornado.iostream.IOStream(s)

    logger.debug('%s: connecting to %s...', self.origurl, addr)
    self.stream.set_close_callback(self.before_connected)

    if self._protocol == 'https':
      self.stream.connect(
        addr, self.send_request,
        server_hostname = addr[0],
      )
    else:
      self.stream.connect(addr, self.send_request)

  def new_url(self, url):
    self.url_visited.append(url)
    self.fullurl = url

    for finder in self._url_finders:
      f = finder.match_url(url, self)
      if f:
        self.finder = f
        f()
        return

    addr = self.parse_url(url)
    if addr != self.addr:
      if self.stream:
        self.stream.close()
      self.new_connection(addr)
    else:
      logger.debug('%s: try to reuse existing connection to %s', self.origurl, self.addr)
      try:
        self.send_request(nocallback=True)
      except tornado.iostream.StreamClosedError:
        logger.debug('%s: server at %s doesn\'t like keep-alive, will reconnect.', self.origurl, self.addr)
        # The close callback should have already run
        self.stream.close()
        self.new_connection(addr)

  def run_callback(self, arg):
    self.io_loop.remove_timeout(self._timeout)
    self._finished = True
    if self.stream:
      self.stream.close()
    self._callback(arg, self)

  def send_request(self, nocallback=False):
    self._connected = True
    req = ['GET %s HTTP/1.1',
           'Host: %s',
           # t.co will return 200 and use js/meta to redirect using the following :-(
           # 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:16.0) Gecko/20100101 Firefox/16.0',
           'User-Agent: %s' % UserAgent,
           'Accept: text/html,application/xhtml+xml;q=0.9,*/*;q=0.7',
           'Accept-Language: zh-cn,zh;q=0.7,en;q=0.3',
           'Accept-Charset: utf-8,gb18030;q=0.7,*;q=0.7',
           'Accept-Encoding: gzip, deflate',
           'Connection: keep-alive',
          ]
    if self.referrer is not None:
      req.append('Referer: ' + self.referrer.replace('%', '%%'))
    path = self.url.path or '/'
    if self.url.query:
      path += '?' + self.url.query
    req = '\r\n'.join(req) % (
      path, self._prepare_host(self.host),
    )
    if self._cookie:
      req += '\r\n' + self._cookie
    req += '\r\n\r\n'
    self.stream.write(req.encode())
    self.headers_done = False
    self.parser = HttpParser(decompress=True)
    if not nocallback:
      self.stream.read_until_close(
        # self.addr and self.stream may have been changed when close callback is run
        partial(self.on_data, close=True, addr=self.addr, stream=self.stream),
        streaming_callback=self.on_data,
      )

  def _prepare_host(self, host):
    if not py3 and isinstance(host, str):
      host = host.decode("utf-8")
    host = encodings.idna.nameprep(host)
    return b'.'.join(encodings.idna.ToASCII(x) if x else b''
                     for x in host.split('.')).decode('ascii')

  def on_data(self, data, close=False, addr=None, stream=None):
    if close:
      logger.debug('%s: connection to %s closed.', self.origurl, addr)

    if self.stream.error:
      self.run_callback(self.stream.error)
      return

    if (close and stream and self._redirected_stream is stream) or self._finished:
      # The connection is closing, and we are being redirected or we're done.
      self._redirected_stream = None
      return

    recved = len(data)
    logger.debug('%s: received data: %d bytes', self.origurl, recved)

    p = self.parser
    nparsed = p.execute(data, recved)
    if close:
      # feed EOF
      p.execute(b'', 0)

    if not self.headers_done and p.is_headers_complete():
      if not self.on_headers_done():
        return

    if p.is_partial_body():
      chunk = p.recv_body()
      if self.finder is None:
        # redirected but has body received
        return
      t = self.feed_finder(chunk)
      if t is not None:
        self.run_callback(t)
        return

    if p.is_message_complete():
      if self.finder is None:
        # redirected but has body received
        return
      t = self.feed_finder(None)
      # if title not found, t is None
      self.run_callback(t)
    elif close:
      self.run_callback(self.stream.error or ConnectionClosed)

  def before_connected(self):
    '''check if something wrong before connected'''
    if not self._connected and not self._finished:
      self.run_callback(self.stream.error)

  def process_cookie(self):
    setcookie = self.headers.get('Set-Cookie', None)
    if not setcookie:
      return

    cookies = _cookie_re.findall(setcookie)
    self._cookie = 'Cookie: ' + '; '.join(cookies)

  def on_headers_done(self):
    '''returns True if should proceed, None if should stop for current chunk'''
    self.headers_done = True
    self.headers = self.parser.get_headers()

    self.status_code = self.parser.get_status_code()
    if self.status_code in (301, 302, 307, 308):
      self.process_cookie() # or we may be redirecting to a loop
      logger.debug('%s: redirect to %s', self.origurl, self.headers['Location'])
      self.followed_times += 1
      if self.followed_times > self.max_follows:
        self.run_callback(TooManyRedirection)
      else:
        newurl = urljoin(self.fullurl, self.headers['Location'])
        self._redirected_stream = self.stream
        self.new_url(newurl)
      return

    try:
      l = int(self.headers.get('Content-Length', None))
    except (ValueError, TypeError):
      l = None

    ctype = self.headers.get('Content-Type', 'text/html')
    mt = defaultMediaType._replace(type=ctype, size=l)
    for finder in self._content_finders:
      f = finder.match_type(mt)
      if f:
        self.finder = f
        break
    else:
      self.run_callback(mt)
      return

    return True

  def feed_finder(self, chunk):
    '''feed data to finder, return the title if found'''
    t = self.finder(chunk)
    if t is not None:
      return t

class URLFinder:
  httpclient = None
  def __init__(self, url, fetcher, match=None):
    self.fullurl = url
    self.match = match
    self.fetcher = fetcher

  @classmethod
  def match_url(cls, url, fetcher):
    if hasattr(cls, '_url_pat'):
      m = cls._url_pat.match(url)
      if m is not None:
        return cls(url, fetcher, m)
    if hasattr(cls, '_match_url') and cls._match_url(url, fetcher):
      return cls(url, fetcher)

  def done(self, info):
    self.fetcher.run_callback(info)

  def get_httpclient(self):
    return self.httpclient or AsyncHTTPClient()

