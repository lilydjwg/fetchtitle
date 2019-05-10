__version__ = '3.0dev'
__url__ = 'https://github.com/lilydjwg/fetchtitle'

import re
import struct
import logging
from collections import namedtuple
from html.parser import HTMLParser
from html.entities import entitydefs
from urllib.parse import urljoin
import asyncio

import aiohttp
import async_timeout

UserAgent = 'FetchTitle/%s (%s)' % (__version__, __url__)

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

def strip_and_collapse_whitespace(s):
  # http://www.w3.org/TR/html5/infrastructure.html#strip-and-collapse-whitespace
  return re.sub('[ \t\n\r\f]+', ' ', s).strip(' \t\n\r\f')

class HtmlTitleParser(HTMLParser):
  charset = title = None
  default_charset = 'utf-8'
  result = None
  _title_coming = False

  def __init__(self):
    # use a list to store literal bytes and escaped Unicode
    super().__init__(convert_charrefs=False)
    self.title = []

  def feed(self, bytesdata):
    if bytesdata:
      data = bytesdata.decode('latin1')
      super().feed(data)
    else:
      self.close()

  def close(self):
    self._check_result(force=True)
    super().close()

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

  def handle_data(self, data, *, unicode=False):
    if not unicode:
      data = data.encode('latin1') # encode back
    if self._title_coming:
      self.title.append(data)

  def handle_endtag(self, tag):
    if tag == 'title':
      self._title_coming = False
      self._check_result()

  def handle_charref(self, name):
    if name[0] in ('x', 'X'):
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

  def _check_result(self, *, force=False):
    if self.result is not None:
      return

    if (force or self.charset is not None) \
       and self.title:
      # always use 'replace' because surrogateescape may not be used elsewhere
      error_handler = 'replace'
      self.result = strip_and_collapse_whitespace(''.join(
        x if isinstance(x, str) else x.decode(
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

TooManyRedirection = SingletonFactory('TooManyRedirection')
Timeout = SingletonFactory('Timeout')
TitleTooFaraway = SingletonFactory('TitleTooFaraway')

logger = logging.getLogger('fetchtitle')

Result = namedtuple(
  'Result',
  'info status_code url_visited finder',
)

class Redirected(Exception):
  def __init__(self, newurl, skip_urlfinder=False):
    self.newurl = newurl
    self.skip_urlfinder = skip_urlfinder

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
        self.blocklen = self.buf[4] * 256 + self.buf[5] + 2
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
  timeout = 15
  max_follows = 10
  _content_finders = (TitleFinder, PNGFinder, JPEGFinder, GIFFinder)
  _url_finders = ()
  __our_session = False
  user_agent = UserAgent

  @property
  def session(self):
    if not self._session:
      s = aiohttp.ClientSession(headers={
        'User-Agent': self.user_agent,
      })
      self.__our_session = True
      self._session = s
    return self._session

  def __init__(self, url, *,
               session=None, timeout=None,
               max_follows=None,
               content_finders=None, url_finders=None):
    self._session = session

    if timeout is not None:
      self.timeout = timeout
    if max_follows is not None:
      self.max_follows = max_follows

    if content_finders is not None:
      self._content_finders = content_finders
    if url_finders is not None:
      self._url_finders = url_finders

    self.url = url
    self.url_visited = []

  async def run(self):
    r = None
    url = self.url
    skip_urlfinder = False

    try:
      async with async_timeout.timeout(self.timeout):
        for _ in range(self.max_follows):
          try:
            r = await self._one_url(
              url, skip_urlfinder=skip_urlfinder)
          except Redirected as e:
            url = e.newurl
            skip_urlfinder = e.skip_urlfinder
            continue
          break
    except asyncio.TimeoutError:
      return Result(Timeout, 0, self.url_visited, None)

    if r is not None:
      return r
    else:
      return Result(
        TooManyRedirection, 0, self.url_visited, None,
      )

  async def _one_url(self, url, skip_urlfinder):
    logger.debug('processing url: %s', url)
    self.url_visited.append(url)

    if not skip_urlfinder:
      for finder in self._url_finders:
        f = finder.match_url(url, self.session)
        if f:
          logger.debug('%r matched with url %s', f, url)
          info = await f.run()
          return Result(info, 0, self.url_visited, f)

    async with self.session.get(
      url, allow_redirects = False, ssl = False,
    ) as r:

      if r.status in (301, 302, 307, 308):
        newurl = r.headers.get('Location')
        newurl = urljoin(url, newurl)
        logger.debug('redirected to %s', newurl)
        raise Redirected(newurl)

      ctype = r.headers.get('Content-Type', 'text/html')
      l = r.headers.get('Content-Length', None)
      if l:
        l = int(l)
      mt = defaultMediaType._replace(type=ctype, size=l)
      logger.debug('media type: %r', mt)
      for finder in self._content_finders:
        f = finder.match_type(mt)
        if f:
          logger.debug('finder %r matches', f)
          break

      if not f:
        return Result(mt, r.status, self.url_visited, None)

      while True:
        data = await r.content.readany()
        t = f(data)

        if t is not None:
          return Result(t, r.status, self.url_visited, f)

        if not data:
          break

      return Result(None, r.status, self.url_visited, f)

  def __del__(self):
    if self.__our_session:
      loop = asyncio.get_event_loop()
      closer = self.session.close()
      if loop.is_running():
        asyncio.ensure_future(closer)
      else:
        asyncio.run(closer)

class URLFinder:
  def __init__(self, url, session, match=None):
    self.session = session
    self.url = url
    self.match = match

  @classmethod
  def match_url(cls, url, session):
    if hasattr(cls, '_url_pat'):
      m = cls._url_pat.match(url)
      if m is not None:
        return cls(url, session, m)
    if hasattr(cls, '_match_url') and \
       cls._match_url(url, session):
      return cls(url, session)

  async def run(self):
    raise NotImplementedError
