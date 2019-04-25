# -*- coding: utf-8 -*-
# This file is part of beets.
# Copyright 2016, Fabrice Laporte
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Abstraction layer to resize images using PIL, ImageMagick, or a
public resizing proxy if neither is available.
"""
from __future__ import division, absolute_import, print_function

import subprocess
import os
import re
from tempfile import NamedTemporaryFile
from six.moves.urllib.parse import urlencode
from beets import logging
from beets import util
import six

# Resizing methods
PIL = 1
IMAGEMAGICK = 2
WEBPROXY = 3

if util.SNI_SUPPORTED:
    PROXY_URL = 'https://images.weserv.nl/'
else:
    PROXY_URL = 'http://images.weserv.nl/'

log = logging.getLogger('beets')


def resize_url(url, maxwidth):
    """Return a proxied image URL that resizes the original image to
    maxwidth (preserving aspect ratio).
    """
    return '{0}?{1}'.format(PROXY_URL, urlencode({
        'url': url.replace('http://', ''),
        'w': maxwidth,
    }))


def temp_file_for(path):
    """Return an unused filename with the same extension as the
    specified path.
    """
    ext = os.path.splitext(path)[1]
    with NamedTemporaryFile(suffix=util.py3_path(ext), delete=False) as f:
        return util.bytestring_path(f.name)


def pil_resize(maxwidth, path_in, path_out=None):
    """Resize using Python Imaging Library (PIL).  Return the output path
    of resized image.
    """
    path_out = path_out or temp_file_for(path_in)
    from PIL import Image
    log.debug(u'artresizer: PIL resizing {0} to {1}',
              util.displayable_path(path_in), util.displayable_path(path_out))

    try:
        im = Image.open(util.syspath(path_in))
        size = maxwidth, maxwidth
        im.thumbnail(size, Image.ANTIALIAS)
        im.save(util.py3_path(path_out))
        return path_out
    except IOError:
        log.error(u"PIL cannot create thumbnail for '{0}'",
                  util.displayable_path(path_in))
        return path_in


def im_resize(maxwidth, path_in, path_out=None):
    """Resize using ImageMagick's ``magick`` tool
    (or fall back to ``convert`` for older versions.)
    Return the output path of resized image.
    """
    path_out = path_out or temp_file_for(path_in)
    log.debug(u'artresizer: ImageMagick resizing {0} to {1}',
              util.displayable_path(path_in), util.displayable_path(path_out))

    # "-resize WIDTHx>" shrinks images with the width larger
    # than the given width while maintaining the aspect ratio
    # with regards to the height.
    try:
        cmd = ArtResizer.shared.im_convert_cmd + \
            [util.syspath(path_in, prefix=False),
                '-resize', '{0}x>'.format(maxwidth),
                util.syspath(path_out, prefix=False)]

        util.command_output(cmd)
    except subprocess.CalledProcessError:
        log.warning(u'artresizer: IM convert failed for {0}',
                    util.displayable_path(path_in))
        return path_in
    return path_out


BACKEND_FUNCS = {
    PIL: pil_resize,
    IMAGEMAGICK: im_resize,
}


def pil_getsize(path_in):
    from PIL import Image
    try:
        im = Image.open(util.syspath(path_in))
        return im.size
    except IOError as exc:
        log.error(u"PIL could not read file {}: {}",
                  util.displayable_path(path_in), exc)


def im_getsize(path_in):
    try:
        cmd = ArtResizer.shared.im_identify_cmd + \
            ['-format', '%w %h', util.syspath(path_in, prefix=False)]

        out = util.command_output(cmd)
    except subprocess.CalledProcessError as exc:
        log.warning(u'ImageMagick size query failed')
        log.debug(
            u'`convert` exited with (status {}) when '
            u'getting size with command {}:\n{}',
            exc.returncode, cmd, exc.output.strip()
        )
        return
    try:
        return tuple(map(int, out.split(b' ')))
    except IndexError:
        log.warning(u'Could not understand IM output: {0!r}', out)


BACKEND_GET_SIZE = {
    PIL: pil_getsize,
    IMAGEMAGICK: im_getsize,
}


class Shareable(type):
    """A pseudo-singleton metaclass that allows both shared and
    non-shared instances. The ``MyClass.shared`` property holds a
    lazily-created shared instance of ``MyClass`` while calling
    ``MyClass()`` to construct a new object works as usual.
    """
    def __init__(cls, name, bases, dict):
        super(Shareable, cls).__init__(name, bases, dict)
        cls._instance = None

    @property
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


class ArtResizer(six.with_metaclass(Shareable, object)):
    """A singleton class that performs image resizes.
    """

    def __init__(self):
        """Create a resizer object with an inferred method.
        """
        self.method = self._check_method()
        log.debug(u"artresizer: method is {0}", self.method)
        self.can_compare = self._can_compare()

        if self.method[0] == IMAGEMAGICK:
            self.im_legacy = self.method[2]
            if self.im_legacy:
                self.im_convert_cmd = ['convert']
                self.im_identify_cmd = ['identify']
            else:
                self.im_convert_cmd = ['magick']
                self.im_identify_cmd = ['magick', 'identify']

    def resize(self, maxwidth, path_in, path_out=None):
        """Manipulate an image file according to the method, returning a
        new path. For PIL or IMAGEMAGIC methods, resizes the image to a
        temporary file. For WEBPROXY, returns `path_in` unmodified.
        """
        if self.local:
            func = BACKEND_FUNCS[self.method[0]]
            return func(maxwidth, path_in, path_out)
        else:
            return path_in

    def proxy_url(self, maxwidth, url):
        """Modifies an image URL according the method, returning a new
        URL. For WEBPROXY, a URL on the proxy server is returned.
        Otherwise, the URL is returned unmodified.
        """
        if self.local:
            return url
        else:
            return resize_url(url, maxwidth)

    @property
    def local(self):
        """A boolean indicating whether the resizing method is performed
        locally (i.e., PIL or ImageMagick).
        """
        return self.method[0] in BACKEND_FUNCS

    def get_size(self, path_in):
        """Return the size of an image file as an int couple (width, height)
        in pixels.

        Only available locally
        """
        if self.local:
            func = BACKEND_GET_SIZE[self.method[0]]
            return func(path_in)

    def _can_compare(self):
        """A boolean indicating whether image comparison is available"""

        return self.method[0] == IMAGEMAGICK and self.method[1] > (6, 8, 7)

    @staticmethod
    def _check_method():
        """Return a tuple indicating an available method and its version."""
        try:
            version, legacy = get_im_version()
            if version > (0, 0, 0):
                return IMAGEMAGICK, version, legacy
        except TypeError:
            pass

        version = get_pil_version()
        if version:
            return PIL, version

        return WEBPROXY, (0)


def get_im_version():
    """Return Image Magick version or None if it is unavailable
    Try invoking ImageMagick's "magick". If "magick" is unavailable,
    as with older versions, fall back to "convert"

    Our iterator will be non-zero when the first command fails, and will
    be returned in a tuple along with the version.
    """
    cmd_names = (['magick'],
                 ['convert'])
    for i, cmd_name in enumerate(cmd_names):

        try:
            cmd = cmd_name + ['--version']
            out = util.command_output(cmd)

            if b'imagemagick' in out.lower():
                pattern = br".+ (\d+)\.(\d+)\.(\d+).*"
                match = re.search(pattern, out)
                version = (int(match.group(1)),
                           int(match.group(2)),
                           int(match.group(3)))
                legacy = bool(i)
                if match:
                    return (version, legacy)

        except (subprocess.CalledProcessError, OSError) as exc:
            log.debug(u'ImageMagick version check failed: {}', exc)

        return ((0,), None)


def get_pil_version():
    """Return Image Magick version or None if it is unavailable
    Try importing PIL."""
    try:
        __import__('PIL', fromlist=[str('Image')])
        return (0,)
    except ImportError:
        return None
