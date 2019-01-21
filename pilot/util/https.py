#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Daniel Drizhuk, d.drizhuk@gmail.com, 2017
# - Mario Lassnig, mario.lassnig@cern.ch, 2017
# - Paul Nilsson, paul.nilsson@cern.ch, 2017

import collections
import commands
import json
import os
import platform
import ssl
import sys
import urllib
import urllib2
import pipes

from .filehandling import write_file

import logging
logger = logging.getLogger(__name__)

_ctx = collections.namedtuple('_ctx', 'ssl_context user_agent capath cacert')


def _tester(func, *args):
    """
    Tests function ``func`` on arguments and returns first positive.

    >>> _tester(lambda x: x%3 == 0, 1, 2, 3, 4, 5, 6)
    3
    >>> _tester(lambda x: x%3 == 0, 1, 2)
    None

    :param func: function(arg)->boolean
    :param args: other arguments
    :return: something or none
    """
    for arg in args:
        if arg is not None and func(arg):
            return arg

    return None


def capath(args=None):
    """
    Tries to get :abbr:`CA (Certification Authority)` path with certificates.
    Testifies it to be a directory.
    Tries next locations:

    1. :option:`--capath` from arguments
    2. :envvar:`X509_CERT_DIR` from env
    3. Path ``/etc/grid-security/certificates``

    :param args: arguments, parsed by `argparse`
    :returns: `str` -- directory path, or `None`
    """

    return _tester(os.path.isdir,
                   None if args is None or args.capath is None else args.capath,
                   os.environ.get('X509_CERT_DIR'),
                   '/etc/grid-security/certificates')


def cacert_default_location():
    """
    Tries to get current user ID through `os.getuid`, and get the posix path for x509 certificate.
    :returns: `str` -- posix default x509 path, or `None`
    """
    try:
        return '/tmp/x509up_u%s' % str(os.getuid())
    except AttributeError:
        logger.warn('No UID available? System not POSIX-compatible... trying to continue')
        pass

    return None


def cacert(args=None):
    """
    Tries to get :abbr:`CA (Certification Authority)` certificate or X509 one.
    Testifies it to be a regular file.
    Tries next locations:

    1. :option:`--cacert` from arguments
    2. :envvar:`X509_USER_PROXY` from env
    3. Path ``/tmp/x509up_uXXX``, where ``XXX`` refers to ``UID``

    :param args: arguments, parsed by `argparse`
    :returns: `str` -- certificate file path, or `None`
    """

    return _tester(os.path.isfile,
                   None if args is None or args.cacert is None else args.capath,
                   os.environ.get('X509_USER_PROXY'),
                   cacert_default_location())


def https_setup(args, version):
    """
    Sets up the context for future HTTPS requests:

    1. Selects the certificate paths
    2. Sets up :mailheader:`User-Agent`
    3. Tries to create `ssl.SSLContext` for future use (falls back to :command:`curl` if fails)

    :param args: arguments, parsed by `argparse`
    :param str version: pilot version string (for :mailheader:`User-Agent`)
    """
    _ctx.user_agent = 'pilot/%s (Python %s; %s %s)' % (version,
                                                       sys.version.split()[0],
                                                       platform.system(),
                                                       platform.machine())
    logger.debug('User-Agent: %s' % _ctx.user_agent)

    _ctx.capath = capath(args)
    _ctx.cacert = cacert(args)

    if sys.version_info < (2, 7, 9):
        logger.warn('Python version <2.7.9 lacks SSL contexts -- falling back to curl')
        _ctx.ssl_context = None
    else:
        try:
            _ctx.ssl_context = ssl.create_default_context(capath=_ctx.capath,
                                                          cafile=_ctx.cacert)
        except Exception as e:
            logger.warn('SSL communication is impossible due to SSL error: %s -- falling back to curl' % str(e))
            _ctx.ssl_context = None


def request(url, data=None, plain=False):
    """
    This function sends a request using HTTPS.
    Sends :mailheader:`User-Agent` and certificates previously being set up by `https_setup`.
    If `ssl.SSLContext` is available, uses `urllib2` as a request processor. Otherwise uses :command:`curl`.

    If ``data`` is provided, encodes it as a URL form data and sends it to the server.

    Treats the request as JSON unless a parameter ``plain`` is `True`.
    If JSON is expected, sends ``Accept: application/json`` header.

    :param string url: the URL of the resource
    :param dict data: data to send
    :param boolean plain: if true, treats the response as a plain text.

    Usage:

    .. code-block:: python
        :emphasize-lines: 2

        https_setup(args, PILOT_VERSION)  # sets up ssl and other stuff
        response = request('https://some.url', {'some':'data'})

    Returns:
        - :keyword:`dict` -- if everything went OK
        - `str` -- if ``plain`` parameter is `True`
        - `None` -- if something went wrong
    """

    _ctx.ssl_context = None  # certificates are not available on the grid, use curl

    logger.debug('server update dictionary = \n%s' % str(data))

    strdata = ""
    for key in data:
        strdata += 'data="%s"\n' % urllib.urlencode({key: data[key]})
    jobId = ''
    if 'jobId' in data.keys():
        jobid = '_%s' % data['jobId']
    # write data to temporary config file
    tmpname = '%s/curl_%s%s.config' % (os.getenv('PILOT_HOME'), os.path.basename(url), jobid)
    s = write_file(tmpname, strdata)
    if not s:
        logger.warning('failed to create curl config file (will attempt to urlencode data directly)')
        dat = pipes.quote(url + '?' + urllib.urlencode(data) if data else '')
    else:
        dat = '--config %s' % tmpname

    if _ctx.ssl_context is None:
        req = 'curl -sS --compressed --connect-timeout %s --max-time %s '\
              '--capath %s --cert %s --cacert %s --key %s '\
              '-H %s %s %s' % (100, 120,
                               pipes.quote(_ctx.capath or ''), pipes.quote(_ctx.cacert or ''),
                               pipes.quote(_ctx.cacert or ''), pipes.quote(_ctx.cacert or ''),
                               pipes.quote('User-Agent: %s' % _ctx.user_agent),
                               "-H " + pipes.quote('Accept: application/json') if not plain else '',
                               dat)
        logger.info('request: %s' % req)
        try:
            status, output = commands.getstatusoutput(req)
        except Exception as e:
            logger.warning('exception: %s' % e)
        else:
            logger.debug('request ok')
        if status != 0:
            logger.warn('request failed (%s): %s' % (status, output))
            return None

        logger.debug('request ok2')
        if plain:
            logger.debug('request ok3')
            return output
        else:
            logger.debug('request ok3')
            try:
                ret = json.loads(output)
            except Exception as e:
                logger.warning('json.loads() failed to parse output=%s: %s' % (output, e))
                return None
            else:
                logger.debug('request ok4')
                return ret
        # return output if plain else json.loads(output)
    else:
        req = urllib2.Request(url, urllib.urlencode(data))
        if not plain:
            req.add_header('Accept', 'application/json')
        req.add_header('User-Agent', _ctx.user_agent)
        try:
            output = urllib2.urlopen(req, context=_ctx.ssl_context)
        except urllib2.HTTPError as e:
            logger.warn('server error (%s): %s' % (e.code, e.read()))
            return None
        except urllib2.URLError as e:
            logger.warn('connection error: %s' % e.reason)
            return None

        return output.read() if plain else json.load(output)
