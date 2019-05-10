import re
import logging
import json

try:
  from lxml.html import fromstring
  has_lxml = True
except ImportError:
  has_lxml = False

from . import (
  URLFinder,
  HtmlTitleParser,
  Redirected,
)

logger = logging.getLogger(__name__)

class GithubFinder(URLFinder):
  _url_pat = re.compile(r'https://github\.com/(?!blog/|showcases/)(?P<repo_path>[^/]+/[^/]+)/?$')
  _api_pat = 'https://api.github.com/repos/{repo_path}'

  @classmethod
  def match_url(cls, url, fetcher):
    if not getattr(fetcher, '_no_github', False):
      return super().match_url(url, fetcher)

  async def run(self):
    m = self.match
    url = self._api_pat.format(**m.groupdict())
    async with self.session.get(url) as res:
      if res.status == 404:
        logger.debug('got 404 from GitHub API, retry with original URL')
        raise Redirected(self.url, skip_urlfinder=True)
      repoinfo = await res.json()
      return repoinfo

class GithubUserFinder(GithubFinder):
  _url_pat = re.compile(r'https://github\.com/(?!blog(?:$|/))(?P<user>[^/]+)/?$')
  _api_pat = 'https://api.github.com/users/{user}'

class SogouImage(URLFinder):
  _url_pat = re.compile(r'http://pinyin\.cn/.+$')
  _img_pat = re.compile(br'"http://input\.shouji\.sogou\.com/multimedia/[^.]+\.jpg"')

  async def run(self):
    async with self.session.get(self.url) as res:
      body = await res.text()
      m = self._img_pat.search(body)
      if m:
        url = self.url = m.group()[1:-1].decode('latin1')
        raise Redirected(url)

class Imagebin(URLFinder):
  _url_pat = re.compile(r'http://imagebin\.org/(\d+)')
  _image_url = 'http://imagebin.org/index.php?mode=image&id='

  async def run(self):
    url = self._image_url + self.match.group(1)
    raise Redirected(url)

class WeixinCopy(URLFinder):
  _url_pat = re.compile(r'http://mp\.weixin\.qq\.com/s\?')
  _src_pat = re.compile(br"var\s+msg_source_url\s+=\s+'([^']+)'")

  async def run(self):
    async with self.session.get(self.url) as res:
      body = await res.text()
      m = self._src_pat.findall(body)
      if m:
        src = m[-1].decode('latin1')
        if src.endswith('#rd'):
          src = src[:-3]
      else:
        src = None

      p = HtmlTitleParser()
      p.feed(res.body)
      if p.result:
        title = p.result
      else:
        title = None
      return title, src

class NeteaseMusic(URLFinder):
  _url_pat = re.compile(r'https?://music\.163\.com/#/(.*)$')

  async def run(self):
    raise Redirected('https://music.163.com/%s' % self.match.group(1))

class ZhihuZhuanlan(URLFinder):
  if has_lxml:
    _url_pat = re.compile(r'https?://zhuanlan\.zhihu\.com/p/(?P<id>\d+)')
  else:
    _url_pat = re.compile(r'a^')

  async def run(self):
    id = self.match.group('id')
    async with self.session.get(self.url) as res:
      page = await res.text()
      doc = fromstring(page)
      static = doc.xpath('//script[@id="js-initialData"]')[0]
      content = json.loads(static.text)['initialState']

      article = content['entities']['articles'][id]
      return article

class RustCrate(URLFinder):
  _url_pat = re.compile(r'https?://crates\.io/crates/(?P<crate>[^/#]+)/?')

  async def run(self):
    url = 'https://crates.io/api/v1/crates/{crate}'
    url = url.format_map(self.match.groupdict())

    async with self.session.get(url) as res:
      info = await res.json()
      return info
