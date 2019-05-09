import asyncio
import ssl

import aiohttp

# source:
# https://github.com/aio-libs/aiohttp/issues/3535#issuecomment-483268542
def ignore_aiohttp_ssl_eror(loop, aiohttpversion='3.5.4'):
  """Ignore aiohttp #3535 issue with SSL data after close

  There appears to be an issue on Python 3.7 and aiohttp SSL that throws a
  ssl.SSLError fatal error (ssl.SSLError: [SSL: KRB5_S_INIT] application data
  after close notify (_ssl.c:2609)) after we are already done with the
  connection. See GitHub issue aio-libs/aiohttp#3535

  Given a loop, this sets up a exception handler that ignores this specific
  exception, but passes everything else on to the previous exception handler
  this one replaces.

  If the current aiohttp version is not exactly equal to aiohttpversion
  nothing is done, assuming that the next version will have this bug fixed.
  This can be disabled by setting this parameter to None

  """
  if aiohttpversion is not None and aiohttp.__version__ != aiohttpversion:
    return

  orig_handler = loop.get_exception_handler() or loop.default_exception_handler

  def ignore_ssl_error(loop, context):
    if context.get('message') == 'SSL error in data received':
      # validate we have the right exception, transport and protocol
      exception = context.get('exception')
      protocol = context.get('protocol')
      if (
        isinstance(exception, ssl.SSLError) and exception.reason == 'KRB5_S_INIT' and
        isinstance(protocol, asyncio.sslproto.SSLProtocol) and
        isinstance(protocol._app_protocol, aiohttp.client_proto.ResponseHandler)
      ):
        if loop.get_debug():
          asyncio.log.logger.debug('Ignoring aiohttp SSL KRB5_S_INIT error')
        return
    orig_handler(context)

  loop.set_exception_handler(ignore_ssl_error)


def fixup():
  ignore_aiohttp_ssl_eror(asyncio.get_running_loop())
