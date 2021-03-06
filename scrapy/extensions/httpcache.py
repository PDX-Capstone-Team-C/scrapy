from __future__ import print_function
import os
import gzip
import zlib
from io import BytesIO
from scrapy.utils.gz import gunzip, is_gzipped
from six.moves import cPickle as pickle
from importlib import import_module
from time import time
from weakref import WeakKeyDictionary
from email.utils import mktime_tz, parsedate_tz
from w3lib.http import headers_raw_to_dict, headers_dict_to_raw
from scrapy.http import Headers, Response
from scrapy.responsetypes import responsetypes
from scrapy.utils.request import request_fingerprint
from scrapy.utils.project import data_path
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.python import to_bytes, to_unicode
from collections import OrderedDict

class DummyPolicy(object):

    def __init__(self, settings):
        self.ignore_schemes = settings.getlist('HTTPCACHE_IGNORE_SCHEMES')
        self.ignore_http_codes = [int(x) for x in settings.getlist('HTTPCACHE_IGNORE_HTTP_CODES')]

    def should_cache_request(self, request):
        return urlparse_cached(request).scheme not in self.ignore_schemes

    def should_cache_response(self, response, request):
        return response.status not in self.ignore_http_codes

    def is_cached_response_fresh(self, response, request):
        return True

    def is_cached_response_valid(self, cachedresponse, response, request):
        return True


class RFC2616Policy(object):

    MAXAGE = 3600 * 24 * 365  # one year

    def __init__(self, settings):
        self.always_store = settings.getbool('HTTPCACHE_ALWAYS_STORE')
        self.ignore_schemes = settings.getlist('HTTPCACHE_IGNORE_SCHEMES')
        self.ignore_response_cache_controls = [to_bytes(cc) for cc in
            settings.getlist('HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS')]
        self._cc_parsed = WeakKeyDictionary()

    def _parse_cachecontrol(self, r):
        if r not in self._cc_parsed:
            cch = r.headers.get(b'Cache-Control', b'')
            parsed = parse_cachecontrol(cch)
            if isinstance(r, Response):
                for key in self.ignore_response_cache_controls:
                    parsed.pop(key, None)
            self._cc_parsed[r] = parsed
        return self._cc_parsed[r]

    def should_cache_request(self, request):
        if urlparse_cached(request).scheme in self.ignore_schemes:
            return False
        cc = self._parse_cachecontrol(request)
        # obey user-agent directive "Cache-Control: no-store"
        if b'no-store' in cc:
            return False
        # Any other is eligible for caching
        return True

    def should_cache_response(self, response, request):
        # What is cacheable - http://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec14.9.1
        # Response cacheability - http://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.4
        # Status code 206 is not included because cache can not deal with partial contents
        cc = self._parse_cachecontrol(response)
        # obey directive "Cache-Control: no-store"
        if b'no-store' in cc:
            return False
        # Never cache 304 (Not Modified) responses
        elif response.status == 304:
            return False
        # Cache unconditionally if configured to do so
        elif self.always_store:
            return True
        # Any hint on response expiration is good
        elif b'max-age' in cc or b'Expires' in response.headers:
            return True
        # Firefox fallbacks this statuses to one year expiration if none is set
        elif response.status in (300, 301, 308):
            return True
        # Other statuses without expiration requires at least one validator
        elif response.status in (200, 203, 401):
            return b'Last-Modified' in response.headers or b'ETag' in response.headers
        # Any other is probably not eligible for caching
        # Makes no sense to cache responses that does not contain expiration
        # info and can not be revalidated
        else:
            return False

    def is_cached_response_fresh(self, cachedresponse, request):
        cc = self._parse_cachecontrol(cachedresponse)
        ccreq = self._parse_cachecontrol(request)
        if b'no-cache' in cc or b'no-cache' in ccreq:
            return False

        now = time()
        freshnesslifetime = self._compute_freshness_lifetime(cachedresponse, request, now)
        currentage = self._compute_current_age(cachedresponse, request, now)

        reqmaxage = self._get_max_age(ccreq)
        if reqmaxage is not None:
            freshnesslifetime = min(freshnesslifetime, reqmaxage)

        if currentage < freshnesslifetime:
            return True

        if b'max-stale' in ccreq and b'must-revalidate' not in cc:
            # From RFC2616: "Indicates that the client is willing to
            # accept a response that has exceeded its expiration time.
            # If max-stale is assigned a value, then the client is
            # willing to accept a response that has exceeded its
            # expiration time by no more than the specified number of
            # seconds. If no value is assigned to max-stale, then the
            # client is willing to accept a stale response of any age."
            staleage = ccreq[b'max-stale']
            if staleage is None:
                return True

            try:
                if currentage < freshnesslifetime + max(0, int(staleage)):
                    return True
            except ValueError:
                pass

        # Cached response is stale, try to set validators if any
        self._set_conditional_validators(request, cachedresponse)
        return False

    def is_cached_response_valid(self, cachedresponse, response, request):
        # Use the cached response if the new response is a server error,
        # as long as the old response didn't specify must-revalidate.
        if response.status >= 500:
            cc = self._parse_cachecontrol(cachedresponse)
            if b'must-revalidate' not in cc:
                return True

        # Use the cached response if the server says it hasn't changed.
        return response.status == 304

    def _set_conditional_validators(self, request, cachedresponse):
        if b'Last-Modified' in cachedresponse.headers:
            request.headers[b'If-Modified-Since'] = cachedresponse.headers[b'Last-Modified']

        if b'ETag' in cachedresponse.headers:
            request.headers[b'If-None-Match'] = cachedresponse.headers[b'ETag']

    def _get_max_age(self, cc):
        try:
            return max(0, int(cc[b'max-age']))
        except (KeyError, ValueError):
            return None

    def _compute_freshness_lifetime(self, response, request, now):
        # Reference nsHttpResponseHead::ComputeFreshnessLifetime
        # http://dxr.mozilla.org/mozilla-central/source/netwerk/protocol/http/nsHttpResponseHead.cpp#410
        cc = self._parse_cachecontrol(response)
        maxage = self._get_max_age(cc)
        if maxage is not None:
            return maxage

        # Parse date header or synthesize it if none exists
        date = rfc1123_to_epoch(response.headers.get(b'Date')) or now

        # Try HTTP/1.0 Expires header
        if b'Expires' in response.headers:
            expires = rfc1123_to_epoch(response.headers[b'Expires'])
            # When parsing Expires header fails RFC 2616 section 14.21 says we
            # should treat this as an expiration time in the past.
            return max(0, expires - date) if expires else 0

        # Fallback to heuristic using last-modified header
        # This is not in RFC but on Firefox caching implementation
        lastmodified = rfc1123_to_epoch(response.headers.get(b'Last-Modified'))
        if lastmodified and lastmodified <= date:
            return (date - lastmodified) / 10

        # This request can be cached indefinitely
        if response.status in (300, 301, 308):
            return self.MAXAGE

        # Insufficient information to compute fresshness lifetime
        return 0

    def _compute_current_age(self, response, request, now):
        # Reference nsHttpResponseHead::ComputeCurrentAge
        # http://dxr.mozilla.org/mozilla-central/source/netwerk/protocol/http/nsHttpResponseHead.cpp#366
        currentage = 0
        # If Date header is not set we assume it is a fast connection, and
        # clock is in sync with the server
        date = rfc1123_to_epoch(response.headers.get(b'Date')) or now
        if now > date:
            currentage = now - date

        if b'Age' in response.headers:
            try:
                age = int(response.headers[b'Age'])
                currentage = max(currentage, age)
            except ValueError:
                pass

        return currentage


class DbmCacheStorage(object):

    def __init__(self, settings):
        self.cachedir = data_path(settings['HTTPCACHE_DIR'], createdir=True)
        self.expiration_secs = settings.getint('HTTPCACHE_EXPIRATION_SECS')
        self.dbmodule = import_module(settings['HTTPCACHE_DBM_MODULE'])
        self.db = None

    def open_spider(self, spider):
        dbpath = os.path.join(self.cachedir, '%s.db' % spider.name)
        self.db = self.dbmodule.open(dbpath, 'c')

    def close_spider(self, spider):
        self.db.close()

    def retrieve_response(self, spider, request):
        data = self._read_data(spider, request)
        if data is None:
            return  # not cached
        url = data['url']
        status = data['status']
        headers = Headers(data['headers'])
        body = data['body']
        respcls = responsetypes.from_args(headers=headers, url=url)
        response = respcls(url=url, headers=headers, status=status, body=body)
        return response

    def store_response(self, spider, request, response):
        key = self._request_key(request)
        data = {
            'status': response.status,
            'url': response.url,
            'headers': dict(response.headers),
            'body': response.body,
        }
        self.db['%s_data' % key] = pickle.dumps(data, protocol=2)
        self.db['%s_time' % key] = str(time())

    def _read_data(self, spider, request):
        key = self._request_key(request)
        db = self.db
        tkey = '%s_time' % key
        if tkey not in db:
            return  # not found

        ts = db[tkey]
        if 0 < self.expiration_secs < time() - float(ts):
            return  # expired

        return pickle.loads(db['%s_data' % key])

    def _request_key(self, request):
        return request_fingerprint(request)


class FilesystemCacheStorage(object):

    def __init__(self, settings):
        self.cachedir = data_path(settings['HTTPCACHE_DIR'])
        self.expiration_secs = settings.getint('HTTPCACHE_EXPIRATION_SECS')
        self.use_gzip = settings.getbool('HTTPCACHE_GZIP')
        self._open = gzip.open if self.use_gzip else open

    def open_spider(self, spider):
        pass

    def close_spider(self, spider):
        pass

    def retrieve_response(self, spider, request):
        """Return response if present in cache, or None otherwise."""
        metadata = self._read_meta(spider, request)
        if metadata is None:
            return  # not cached
        rpath = self._get_request_path(spider, request)
        with self._open(os.path.join(rpath, 'response_body'), 'rb') as f:
            body = f.read()
        with self._open(os.path.join(rpath, 'response_headers'), 'rb') as f:
            rawheaders = f.read()
        url = metadata.get('response_url')
        status = metadata['status']
        headers = Headers(headers_raw_to_dict(rawheaders))
        respcls = responsetypes.from_args(headers=headers, url=url)
        response = respcls(url=url, headers=headers, status=status, body=body)
        return response

    def store_response(self, spider, request, response):
        """Store the given response in the cache."""
        rpath = self._get_request_path(spider, request)
        if not os.path.exists(rpath):
            os.makedirs(rpath)
        metadata = {
            'url': request.url,
            'method': request.method,
            'status': response.status,
            'response_url': response.url,
            'timestamp': time(),
        }
        with self._open(os.path.join(rpath, 'meta'), 'wb') as f:
            f.write(to_bytes(repr(metadata)))
        with self._open(os.path.join(rpath, 'pickled_meta'), 'wb') as f:
            pickle.dump(metadata, f, protocol=2)
        with self._open(os.path.join(rpath, 'response_headers'), 'wb') as f:
            f.write(headers_dict_to_raw(response.headers))
        with self._open(os.path.join(rpath, 'response_body'), 'wb') as f:
            f.write(response.body)
        with self._open(os.path.join(rpath, 'request_headers'), 'wb') as f:
            f.write(headers_dict_to_raw(request.headers))
        with self._open(os.path.join(rpath, 'request_body'), 'wb') as f:
            f.write(request.body)

    def _get_request_path(self, spider, request):
        key = request_fingerprint(request)
        return os.path.join(self.cachedir, spider.name, key[0:2], key)

    def _read_meta(self, spider, request):
        rpath = self._get_request_path(spider, request)
        metapath = os.path.join(rpath, 'pickled_meta')
        if not os.path.exists(metapath):
            return  # not found
        mtime = os.stat(rpath).st_mtime
        if 0 < self.expiration_secs < time() - mtime:
            return  # expired
        with self._open(metapath, 'rb') as f:
            return pickle.load(f)


class DeltaLeveldbCacheStorage(object):

    def __init__(self, settings):
        import leveldb
        import bsdiff4
        self._leveldb = leveldb
        self._bsdiff = bsdiff4
        self.cachedir = data_path(settings['HTTPCACHE_DIR'], createdir=True)
        self.expiration_secs = settings.getint('HTTPCACHE_EXPIRATION_SECS')
        self.db = None
        # List of properties from request and response objects to store in cache
        self.response_to_cache = ['status', 'url', 'headers', 'body']

    def open_spider(self, spider):
        # Set up the old source response if it exists.
        dbpath = os.path.join(self.cachedir, '%s.leveldb' % spider.name)
        self.db = self._leveldb.LevelDB(dbpath)

    def close_spider(self, spider):
        # Do compactation each time to save space and also recreate files to
        # avoid them being removed in storages with timestamp-based autoremoval.
        self.db.CompactRange()
        del self.db

    def retrieve_response(self, spider, request):
        domain = self._parse_domain_from_url(spider, request)
        sources = self._read_data(key_to_use=domain)
        # Explicitly declare these as None, since they're used for controlling logic.
        delta_response = None
        serial_response = None
        data = None
        delta_response = self._read_data(request_to_use=request)
        # Check if we have some sources to look through and if we have a previous delta
        if sources and delta_response:
            sources = pickle.loads(sources)
            # Grab our key
            target_key = self._request_key(request)
            # if our key is a source, no need to look for a source -- full serialized response is stored
            if target_key in sources:
                serial_response = delta_response 
            else:
                # Iterate over every source in our sources
                for source in sources.keys():
                    # If we found our request's key in our sources, decode it and stop looking.
                    if target_key in sources[source]:
                        serial_source = self._read_data(key_to_use=source)
                        serial_response = self._decode_response(delta_response, serial_source)
        # If this condition is true, we didn't find a cached response and return
        if not serial_response:
            return
        data = self._deserialize(serial_response)
        response = self._reconstruct_response(data)
        response = self._recompress(response)
        return response

    def store_response(self, spider, request, response):
        target_key = self._request_key(request)
        response = self._decompress(response)
        target_response = self._serialize(response)
        # use this to control if we write a length or not
        original_length = None
        domain = self._parse_domain_from_url(spider, request)
        # get the pickled data structure of sources from the db
        sources = self._read_data(key_to_use=domain, ignore_time=True)
        # if we have sources, grab a source and delta against it:
        if sources:
            sources = pickle.loads(sources)
            # If we're a source response, check if we're different than what's
            # in the DB. If we are, recompute the deltas for the targets associated
            # with the source
            if target_key in sources:
                # Grab the original
                source_response = self._read_data(key_to_use=target_key, ignore_time=True)
                # Recompute all deltas against the new response
                self._recompute_deltas(target_response, source_response, sources[target_key])
            # Otherwise we store the response as usual
            else:
                # Select an appropriate source
                source_key = self._select_source(target_response, sources)
                # get the source from the db directly. We don't need an associated
                # request because we already have the fingerprint/key.
                source_response = self._read_data(key_to_use=source_key, ignore_time=True)
                # overwrite target_response ref with the delta
                target_response = self._encode_response(target_response, source_response)
                # add the target's key to the source's set
                sources[source_key].add(target_key)
        # otherwise create a new dictionary for our sources and use the current
        # response as a source
        else:
            sources = {target_key: set()}
        # Write the changes out to the db.
        # - In the event that we don't have any sources for this domain,
        #   we'll write out the serialized response and the sources
        #   dict we just created.
        # - If we do have sources, we'll write out the delta'd response and the
        #   change to the sources dict.
        batch = self._leveldb.WriteBatch()
        batch.Put(target_key + b'_data', target_response)
        batch.Put(target_key + b'_time', to_bytes(str(time())))
        batch.Put(domain + b'_data', pickle.dumps(sources, protocol=2))
        batch.Put(domain + b'_time', to_bytes(str(time())))
        self.db.Write(batch)

    # Placeholder for now
    def _parse_domain_from_url(self, spider, request):
        return urlparse_cached(request).hostname or spider.name

    # Placeholder for now
    def _select_source(self, target, sources):
        return sources.keys()[0]

    # Placeholder for now
    def _recompute_deltas(self, new_source, old_source, target_set):
        for target_key in target_set:
            # Get old response from the db
            old_response = self._read_data(key_to_use=target_key, ignore_time=True)
            # Decode serialized delta response with old source
            target_response = self._decode_response(old_response, old_source)
            # Encode old response with new source
            new_delta = self._encode_response(target_response, new_source)
            # Write new target responses to db
            batch = self._leveldb.WriteBatch()
            batch.Put(target_key + b'_data', new_delta)
            self.db.Write(batch)

    def _reconstruct_response(self, data):
        url = data['url']
        status = data['status']
        headers = Headers(data['headers'])
        body = data['body']
        respcls = responsetypes.from_args(headers=headers, url=url)
        response = respcls(url=url, headers=headers, status=status, body=body)
        return response

    def _encode_response(self, target, source):
        delta_contents = self._bsdiff.diff(source, target)
        return delta_contents

    def _decode_response(self, delta, source):
        restored_contents = self._bsdiff.patch(source, delta)
        return restored_contents

    def _serialize(self, response):
        dict_response = OrderedDict()
        for k in self.response_to_cache:
            dict_response[k] = getattr(response, k)
        return pickle.dumps(dict_response, 2)

    def _deserialize(self, serial_response):
        return pickle.loads(serial_response)

    def _recompress(self, response):
        content_encoding = response.headers.getlist('Content-Encoding')
        if content_encoding and not is_gzipped(response):
            encoding = content_encoding[-1].lower()
            if encoding == b'gzip' or encoding == b'x-gzip':
                buffer = BytesIO()
                with gzip.GzipFile(mode='wb', fileobj=buffer) as f:
                    f.write(response.body)
                    f.close()
                encoded_body = buffer.getvalue()
            if encoding == b'deflate':
                encoded_body = zlib.compress(response.body)
            response = response.replace(**{'body': encoded_body})
        return response

    def _decompress(self, response):
        content_encoding = response.headers.getlist('Content-Encoding')
        if content_encoding and not is_gzipped(response):
            encoding = content_encoding[-1].lower()
            if encoding == b'gzip' or encoding == b'x-gzip':
                decoded_body = gunzip(response.body)
            if encoding == b'deflate':
                try:
                    decoded_body = zlib.decompress(response.body)
                except zlib.error:
                    decoded_body = zlib.decompress(response.body, -15)
            response = response.replace(**{'body': decoded_body})
        return response

    # We can use this when we already have a key ahead of time,
    # i.e. grabbing sources by IP/domain, grabbing a source response.
    def _read_data(self, request_to_use=None, key_to_use=None, ignore_time=False):
        if key_to_use:
            key = key_to_use
        else:
            key = self._request_key(request_to_use)
        if not ignore_time:
            try:
                ts = self.db.Get(key + b'_time')
            except KeyError:
                return  # not found or invalid entry
            if 0 < self.expiration_secs < time() - float(ts):
                return  # expired
        try:
            data = self.db.Get(key + b'_data')
        except KeyError:
            return  # invalid entry
        else:
            return data

    def _request_key(self, request):
        return to_bytes(request_fingerprint(request))


class LeveldbCacheStorage(object):

    def __init__(self, settings):
        import leveldb
        self._leveldb = leveldb
        self.cachedir = data_path(settings['HTTPCACHE_DIR'], createdir=True)
        self.expiration_secs = settings.getint('HTTPCACHE_EXPIRATION_SECS')
        self.db = None

    def open_spider(self, spider):
        dbpath = os.path.join(self.cachedir, '%s.leveldb' % spider.name)
        self.db = self._leveldb.LevelDB(dbpath)

    def close_spider(self, spider):
        # Do compactation each time to save space and also recreate files to
        # avoid them being removed in storages with timestamp-based autoremoval.
        self.db.CompactRange()
        del self.db

    def retrieve_response(self, spider, request):
        data = self._read_data(spider, request)
        if data is None:
            return  # not cached
        url = data['url']
        status = data['status']
        headers = Headers(data['headers'])
        body = data['body']
        respcls = responsetypes.from_args(headers=headers, url=url)
        response = respcls(url=url, headers=headers, status=status, body=body)
        return response

    def store_response(self, spider, request, response):
        key = self._request_key(request)
        data = {
            'status': response.status,
            'url': response.url,
            'headers': dict(response.headers),
            'body': response.body,
        }
        batch = self._leveldb.WriteBatch()
        batch.Put(key + b'_data', pickle.dumps(data, protocol=2))
        batch.Put(key + b'_time', to_bytes(str(time())))
        self.db.Write(batch)

    def _read_data(self, spider, request):
        key = self._request_key(request)
        try:
            ts = self.db.Get(key + b'_time')
        except KeyError:
            return  # not found or invalid entry

        if 0 < self.expiration_secs < time() - float(ts):
            return  # expired

        try:
            data = self.db.Get(key + b'_data')
        except KeyError:
            return  # invalid entry
        else:
            return pickle.loads(data)

    def _request_key(self, request):
        return to_bytes(request_fingerprint(request))



def parse_cachecontrol(header):
    """Parse Cache-Control header

    http://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.9

    >>> parse_cachecontrol(b'public, max-age=3600') == {b'public': None,
    ...                                                 b'max-age': b'3600'}
    True
    >>> parse_cachecontrol(b'') == {}
    True

    """
    directives = {}
    for directive in header.split(b','):
        key, sep, val = directive.strip().partition(b'=')
        if key:
            directives[key.lower()] = val if sep else None
    return directives


def rfc1123_to_epoch(date_str):
    try:
        date_str = to_unicode(date_str, encoding='ascii')
        return mktime_tz(parsedate_tz(date_str))
    except Exception:
        return None
