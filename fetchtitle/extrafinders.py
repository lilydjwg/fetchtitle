import json
import re

from . import (
  URLFinder,
  HtmlTitleParser,
  UserAgent,
)

def _prepare_field(d, key, prefix):
  d[key] = prefix + d[key] if d.get(key, False) else ''

class GithubFinder(URLFinder):
  _url_pat = re.compile(r'https://github\.com/(?!blog/)(?P<repo_path>[^/]+/[^/]+)/?$')
  _api_pat = 'https://api.github.com/repos/{repo_path}'

  def __call__(self):
    m = self.match
    self.get_httpclient().fetch(
      self._api_pat.format(**m.groupdict()), self.parse_info,
      headers = {
        'User-Agent': UserAgent,
      })

  def parse_info(self, res):
    if res.error:
      self.done(res.error)
      return
    repoinfo = json.loads(res.body.decode('utf-8'))
    self.response = res
    self.done(repoinfo)

class GithubUserFinder(GithubFinder):
  _url_pat = re.compile(r'https://github\.com/(?!blog(?:$|/))(?P<user>[^/]+)/?$')
  _api_pat = 'https://api.github.com/users/{user}'

class SogouImage(URLFinder):
  _url_pat = re.compile(r'http://pinyin\.cn/.+$')
  _img_pat = re.compile(br'"http://input\.shouji\.sogou\.com/multimedia/[^.]+\.jpg"')

  def __call__(self):
    self.get_httpclient().fetch(self.fullurl, self._got_page)

  def _got_page(self, res):
    m = self._img_pat.search(res.body)
    if m:
      url = self.url = m.group()[1:-1].decode('latin1')
      self.fetcher.clone(url, self._got_image)

  def _got_image(self, info, fetcher):
    self.done(info)

class Imagebin(URLFinder):
  _url_pat = re.compile(r'http://imagebin\.org/(\d+)')
  _image_url = 'http://imagebin.org/index.php?mode=image&id='

  def __call__(self):
    url = self._image_url + self.match.group(1)
    self.fetcher.clone(url, self._got_info)

  def _got_info(self, info, fetcher):
    self.done(info)

class WeixinCopy(URLFinder):
  _url_pat = re.compile(r'http://mp\.weixin\.qq\.com/s\?')
  _src_pat = re.compile(br"var\s+msg_source_url\s+=\s+'([^']+)'")

  def __call__(self):
    self.get_httpclient().fetch(self.fullurl, self._got_page)

  def _got_page(self, res):
    m = self._src_pat.findall(res.body)
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
    self.done((title, src))

