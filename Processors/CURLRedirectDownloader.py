#!/usr/bin/python
#
# Copyright 2015 Greg Neagle
# Modifications 2015 by Sam Novak
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""See docstring for URLDownloader class"""

import os.path
import re
import subprocess
import time
# import urllib2
import xattr
# import zlib

from autopkglib import Processor, ProcessorError

try:
    from autopkglib import BUNDLE_ID
except ImportError:
    BUNDLE_ID = "com.github.autopkg"

__all__ = ["CURLRedirectDownloader"]

# XATTR names for Etag and Last-Modified headers
XATTR_ETAG = "%s.etag" % BUNDLE_ID
XATTR_LAST_MODIFIED = "%s.last-modified" % BUNDLE_ID


def getxattr(pathname, attr):
    """Get a named xattr from a file. Return None if not present"""
    if attr in xattr.listxattr(pathname):
        return xattr.getxattr(pathname, attr)
    else:
        return None


# Sam N. - I set the filename to true because redirected outputs
# may not lead to exact filenames, so one must be specified.
class CURLRedirectDownloader(Processor):
    """Downloads a URL to the specified download_dir using curl."""
    description = __doc__
    input_variables = {
        "url": {
            "required": True,
            "description": "The URL to download.",
        },
        "request_headers": {
            "required": False,
            "description":
                ("Optional dictionary of headers to include with the download "
                 "request.")
        },
        "download_dir": {
            "required": False,
            "description":
                ("The directory where the file will be downloaded to. Defaults "
                 "to RECIPE_CACHE_DIR/downloads."),
        },
        "filename": {
            "required": True,
            "description": "Filename to download the curl output to.",
        },
        "PKG": {
            "required": False,
            "description":
                ("Local path to the pkg/dmg we'd otherwise download. "
                 "If provided, the download is skipped and we just use "
                 "this package or disk image."),
        },
        "CURL_PATH": {
            "required": False,
            "default": "/usr/bin/curl",
            "description": "Path to curl binrary. Defaults to /usr/bin/curl.",
        },
    }
    output_variables = {
        "pathname": {
            "description": "Path to the downloaded file.",
        },
        "last_modified": {
            "description": "last-modified header for the downloaded item.",
        },
        "etag": {
            "description": "etag header for the downloaded item.",
        },
        "download_changed": {
            "description":
                ("Boolean indicating if the download has changed since the "
                 "last time it was downloaded."),
        },
        "url_downloader_summary_result": {
            "description": "Description of interesting results."
        },
    }

    def main(self):
        # clear any pre-exising summary result
        if 'url_downloader_summary_result' in self.env:
            del self.env['url_downloader_summary_result']

        self.env["last_modified"] = ""
        self.env["etag"] = ""

        if "PKG" in self.env:
            self.env["pathname"] = os.path.expanduser(self.env["PKG"])
            self.env["download_changed"] = True
            self.output("Given %s, no download needed." % self.env["pathname"])
            return

        if not "filename" in self.env:
            # Generate filename.
            filename = self.env["url"].rpartition("/")[2]
        else:
            filename = self.env["filename"]
        download_dir = (self.env.get("download_dir") or
                        os.path.join(self.env["RECIPE_CACHE_DIR"], "downloads"))
        pathname = os.path.join(download_dir, filename)
        # Save pathname to environment
        self.env["pathname"] = pathname

        # create download_dir if needed
        if not os.path.exists(download_dir):
            try:
                os.makedirs(download_dir)
            except OSError, err:
                raise ProcessorError(
                    "Can't create %s: %s" % (download_dir, err.strerror))

        # construct curl command.
        # Sam N. - This curl is used to process headers, to determine the final download location.
        curl_cmd = [self.env['CURL_PATH'], '-L',
                    '--silent', '--show-error', '--no-buffer',
                    '-I',
                    '--speed-time', '30',
                    '--url', self.env["url"]]

        if "request_headers" in self.env:
            headers = self.env["request_headers"]
            for header, value in headers.items():
                curl_cmd.extend(['--header', '%s: %s' % (header, value)])

        # if file already exists, add some headers to the request
        # so we don't retrieve the content if it hasn't changed
        if os.path.exists(pathname):
            etag = getxattr(pathname, XATTR_ETAG)
            last_modified = getxattr(pathname, XATTR_LAST_MODIFIED)
            existing_file_length = os.path.getsize(pathname)
            if etag:
                curl_cmd.extend(['--header', 'If-None-Match: %s' % etag])
            if last_modified:
                curl_cmd.extend(
                ['--header', 'If-Modified-Since: %s' % last_modified])

        # Open URL.
        proc = subprocess.Popen(curl_cmd, shell=False, bufsize=1,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

        donewithheaders = False
        maxheaders = 15
        header = {}
        downloadlocation = ""
        while True:
            if not donewithheaders:
                info = proc.stdout.readline().strip('\r\n')
                if info.startswith('HTTP/'):
                    header['http_result_code'] = info.split(None, 2)[1]
                    header['http_result_description'] = info.split(None, 2)[2]
                elif ': ' in info:
                    # got a header line
                    part = info.split(None, 1)
                    fieldname = part[0].rstrip(':').lower()
                    header[fieldname] = part[1]
                elif info == '':
                    # we got an empty line; end of headers (or curl exited)
                    if header.get('http_result_code') in ['301', '302', '303']:
                        # redirect, so more headers are coming.
                        # Throw away the headers we've received so far
                        # Sam N. - Store the final download location for another download loop
                        downloadlocation = header['location']
                        header = {}
                        header['http_result_code'] = '000'
                        header['http_result_description'] = ''
                    else:
                        donewithheaders = True
            else:
                time.sleep(0.1)

            if proc.poll() != None:
                # For small download files curl may exit before all headers
                # have been parsed, don't immediately exit.
                maxheaders -= 1
                if donewithheaders or maxheaders <= 0:
                    break

        # Use a second command for the actual download
        curl_cmd2 = [self.env['CURL_PATH'], '-L',
                     '--silent', '--show-error', '--no-buffer',
                     '--speed-time', '30',
                     '--dump-header', '-',
                     '--output', pathname,
                     '--url', downloadlocation]

        retcode = proc.poll()
        if retcode:
            curlerr = ''
            try:
                curlerr = proc.stderr.read().rstrip('\n')
                curlerr = curlerr.split(None, 2)[2]
            except IndexError:
                pass
            if retcode == 22:
                # 22 means any 400 series return code. Note: header seems not to
                # be dumped to STDOUT for immediate failures. Hence
                # http_result_code is likely blank/000. Read it from stderr.
                if re.search(r'URL returned error: [0-9]+$', curlerr):
                    header['http_result_code'] = curlerr[curlerr.rfind(' ') + 1:]

        if header['http_result_code'] == '304':
            # resource not modified
            self.env["download_changed"] = False
            self.output("Item at URL is unchanged.")
            self.output("Using existing %s" % pathname)
            return
        else:
            # Download the file - This second loop does the actual downloading of the file.
            proc2 = subprocess.Popen(curl_cmd2, shell=False, bufsize=1,
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            donewithheaders2 = False
            maxheaders2 = 15
            header = {}
            while True:
                if not donewithheaders2:
                    info = proc2.stdout.readline().strip('\r\n')
                    if info.startswith('HTTP/'):
                        header['http_result_code'] = info.split(None, 2)[1]
                        header['http_result_description'] = info.split(None, 2)[2]
                    elif ': ' in info:
                        # got a header line
                        part = info.split(None, 1)
                        fieldname = part[0].rstrip(':').lower()
                        header[fieldname] = part[1]
                    elif info == '':
                        # we got an empty line; end of headers (or curl exited)
                        # if header.get('http_result_code') in ['301', '302', '303']:
                        #     # redirect, so more headers are coming.
                        #     # Throw away the headers we've received so far
                        #     header = {}
                        #     header['http_result_code'] = '000'
                        #     header['http_result_description'] = ''
                        # else:
                            donewithheaders2 = True
                else:
                    time.sleep(0.1)

                if proc2.poll() != None:
                    # For small download files curl may exit before all headers
                    # have been parsed, don't immediately exit.
                    maxheaders2 -= 1
                    if donewithheaders2 or maxheaders2 <= 0:
                        break

        self.env["download_changed"] = True

        # save last-modified header if it exists
        if header.get("last-modified"):
            self.env["last_modified"] = (
                header.get("last-modified"))
            xattr.setxattr(
                pathname, XATTR_LAST_MODIFIED,
                header.get("last-modified"))
            self.output(
                "Storing new Last-Modified header: %s"
                % header.get("last-modified"))

        # save etag if it exists
        self.env["etag"] = ""
        if header.get("etag"):
            self.env["etag"] = header.get("etag")
            xattr.setxattr(
                pathname, XATTR_ETAG, header.get("etag"))
            self.output("Storing new ETag header: %s"
                        % header.get("etag"))

        self.output("Downloaded %s" % pathname)
        self.env['url_downloader_summary_result'] = {
            'summary_text': 'The following new items were downloaded:',
            'data': {
                'download_path': pathname,
            }
        }


if __name__ == "__main__":
    PROCESSOR = CURLRedirectDownloader()
    PROCESSOR.execute_shell()
