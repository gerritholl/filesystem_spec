import hashlib
import inspect
import logging
import os
import pickle
import tempfile
import time
from shutil import move, rmtree

from fsspec import AbstractFileSystem, filesystem
from fsspec.compression import compr
from fsspec.core import BaseCache, MMapCache
from fsspec.spec import AbstractBufferedFile
from fsspec.utils import infer_compression

logger = logging.getLogger("fsspec")


class CachingFileSystem(AbstractFileSystem):
    """Locally caching filesystem, layer over any other FS

    This class implements chunk-wise local storage of remote files, for quick
    access after the initial download. The files are stored in a given
    directory with random hashes for the filenames. If no directory is given,
    a temporary one is used, which should be cleaned up by the OS after the
    process ends. The files themselves as sparse (as implemented in
    MMapCache), so only the data which is accessed takes up space.

    Restrictions:

    - the block-size must be the same for each access of a given file, unless
      all blocks of the file have already been read
    - caching can only be applied to file-systems which produce files
      derived from fsspec.spec.AbstractBufferedFile ; LocalFileSystem is also
      allowed, for testing
    """

    protocol = ("blockcache", "cached")

    def __init__(
        self,
        target_protocol=None,
        cache_storage="TMP",
        cache_check=10,
        check_files=False,
        expiry_time=604800,
        target_options=None,
        fs=None,
        same_names=False,
        compression=None,
        **kwargs,
    ):
        """

        Parameters
        ----------
        target_protocol: str (optional)
            Target filesystem protocol. Provide either this or ``fs``.
        cache_storage: str or list(str)
            Location to store files. If "TMP", this is a temporary directory,
            and will be cleaned up by the OS when this process ends (or later).
            If a list, each location will be tried in the order given, but
            only the last will be considered writable.
        cache_check: int
            Number of seconds between reload of cache metadata
        check_files: bool
            Whether to explicitly see if the UID of the remote file matches
            the stored one before using. Warning: some file systems such as
            HTTP cannot reliably give a unique hash of the contents of some
            path, so be sure to set this option to False.
        expiry_time: int
            The time in seconds after which a local copy is considered useless.
            Set to falsy to prevent expiry. The default is equivalent to one
            week.
        target_options: dict or None
            Passed to the instantiation of the FS, if fs is None.
        fs: filesystem instance
            The target filesystem to run against. Provide this or ``protocol``.
        same_names: bool (optional)
            By default, target URLs are hashed, so that files from different backends
            with the same basename do not conflict. If this is true, the original
            basename is used.
        compression: str (optional)
            To decompress on download. Can be 'infer' (guess from the URL name),
            one of the entries in ``fsspec.compression.compr``, or None for no
            decompression.
        """
        super().__init__(**kwargs)
        if not (fs is None) ^ (target_protocol is None):
            raise ValueError(
                "Please provide one of filesystem instance (fs) or"
                " remote_protocol, not both"
            )
        if cache_storage == "TMP":
            storage = [tempfile.mkdtemp()]
        else:
            if isinstance(cache_storage, str):
                storage = [cache_storage]
            else:
                storage = cache_storage
        os.makedirs(storage[-1], exist_ok=True)
        self.storage = storage
        self.kwargs = target_options or {}
        self.cache_check = cache_check
        self.check_files = check_files
        self.expiry = expiry_time
        self.compression = compression
        # TODO: same_names should allow for variable prefix, not only
        #  to keep the basename
        self.same_names = same_names
        self.target_protocol = (
            target_protocol
            if isinstance(target_protocol, str)
            else (fs.protocol if isinstance(fs.protocol, str) else fs.protocol[0])
        )
        self.load_cache()
        self.fs = fs if fs is not None else filesystem(target_protocol, **self.kwargs)

        def _strip_protocol(path):
            # acts as a method, since each instance has a difference target
            return self.fs._strip_protocol(type(self)._strip_protocol(path))

        self._strip_protocol = _strip_protocol

    def _mkcache(self):
        os.makedirs(self.storage[-1], exist_ok=True)

    def load_cache(self):
        """Read set of stored blocks from file"""
        cached_files = []
        for storage in self.storage:
            fn = os.path.join(storage, "cache")
            if os.path.exists(fn):
                with open(fn, "rb") as f:
                    # TODO: consolidate blocks here
                    loaded_cached_files = pickle.load(f)
                    for c in loaded_cached_files.values():
                        if isinstance(c["blocks"], list):
                            c["blocks"] = set(c["blocks"])
                    cached_files.append(loaded_cached_files)
            else:
                cached_files.append({})
        self._mkcache()
        self.cached_files = cached_files or [{}]
        self.last_cache = time.time()

    def save_cache(self):
        """Save set of stored blocks from file"""
        fn = os.path.join(self.storage[-1], "cache")
        # TODO: a file lock could be used to ensure file does not change
        #  between re-read and write; but occasional duplicated reads ok.
        cache = self.cached_files[-1]
        if os.path.exists(fn):
            with open(fn, "rb") as f:
                cached_files = pickle.load(f)
            for k, c in cached_files.items():
                if k in cache:
                    if c["blocks"] is True or cache[k]["blocks"] is True:
                        c["blocks"] = True
                    else:
                        c["blocks"] = set(c["blocks"]).union(cache[k]["blocks"])
                    c["time"] = max(c["time"], cache[k]["time"])

            # Files can be added to cache after it was written once
            for k, c in cache.items():
                if k not in cached_files:
                    cached_files[k] = c
        else:
            cached_files = cache
        cache = {k: v.copy() for k, v in cached_files.items()}
        for c in cache.values():
            if isinstance(c["blocks"], set):
                c["blocks"] = list(c["blocks"])
        fn2 = tempfile.mktemp()
        with open(fn2, "wb") as f:
            pickle.dump(cache, f)
        self._mkcache()
        move(fn2, fn)
        self.cached_files[-1] = cached_files
        self.last_cache = time.time()

    def _check_cache(self):
        """Reload caches if time elapsed or any disappeared"""
        self._mkcache()
        if not self.cache_check:
            # explicitly told not to bother checking
            return
        timecond = time.time() - self.last_cache > self.cache_check
        existcond = all(os.path.exists(storage) for storage in self.storage)
        if timecond or not existcond:
            self.load_cache()

    def _check_file(self, path):
        """Is path in cache and still valid"""
        path = self._strip_protocol(path)

        self._check_cache()
        for storage, cache in zip(self.storage, self.cached_files):
            if path not in cache:
                continue
            detail = cache[path].copy()
            if self.check_files:
                if detail["uid"] != self.fs.ukey(path):
                    continue
            if self.expiry:
                if time.time() - detail["time"] > self.expiry:
                    continue
            fn = os.path.join(storage, detail["fn"])
            if os.path.exists(fn):
                return detail, fn
        return False

    def clear_cache(self):
        """Remove all files and metadat from the cache

        In the case of multiple cache locations, this clears only the last one,
        which is assumed to be the read/write one.
        """
        rmtree(self.storage[-1])
        self.load_cache()

    def pop_from_cache(self, path):
        """Remove cached version of given file

        Deletes local copy of the given (remote) path. If it is found in a cache
        location which is not the last, it is assumed to be read-only, and
        raises PermissionError
        """
        path = self._strip_protocol(path)
        _, fn = self._check_file(path)
        if fn is None:
            return
        if fn.startswith(self.storage[-1]):
            # is in in writable cache
            os.remove(fn)
            self.cached_files[-1].pop(path)
            self.save_cache()
        else:
            raise PermissionError(
                "Can only delete cached file in last, writable cache location"
            )

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        **kwargs,
    ):
        """Wrap the target _open

        If the whole file exists in the cache, just open it locally and
        return that.

        Otherwise, open the file on the target FS, and make it have a mmap
        cache pointing to the location which we determine, in our cache.
        The ``blocks`` instance is shared, so as the mmap cache instance
        updates, so does the entry in our ``cached_files`` attribute.
        We monkey-patch this file, so that when it closes, we call
        ``close_and_update`` to save the state of the blocks.
        """
        path = self._strip_protocol(path)

        path = self.fs._strip_protocol(path)
        if "r" not in mode:
            return self.fs._open(
                path,
                mode=mode,
                block_size=block_size,
                autocommit=autocommit,
                cache_options=cache_options,
                **kwargs,
            )
        detail = self._check_file(path)
        if detail:
            # file is in cache
            detail, fn = detail
            hash, blocks = detail["fn"], detail["blocks"]
            if blocks is True:
                # stored file is complete
                logger.debug("Opening local copy of %s" % path)
                return open(fn, mode)
            # TODO: action where partial file exists in read-only cache
            logger.debug("Opening partially cached copy of %s" % path)
        else:
            hash = self.hash_name(path, self.same_names)
            fn = os.path.join(self.storage[-1], hash)
            blocks = set()
            detail = {
                "fn": hash,
                "blocks": blocks,
                "time": time.time(),
                "uid": self.fs.ukey(path),
            }
            self.cached_files[-1][path] = detail
            logger.debug("Creating local sparse file for %s" % path)

        # call target filesystems open
        self._mkcache()
        f = self.fs._open(
            path,
            mode=mode,
            block_size=block_size,
            autocommit=autocommit,
            cache_options=cache_options,
            cache_type=None,
            **kwargs,
        )
        if self.compression:
            comp = (
                infer_compression(path)
                if self.compression == "infer"
                else self.compression
            )
            f = compr[comp](f, mode="rb")
        if "blocksize" in detail:
            if detail["blocksize"] != f.blocksize:
                raise ValueError(
                    "Cached file must be reopened with same block"
                    "size as original (old: %i, new %i)"
                    "" % (detail["blocksize"], f.blocksize)
                )
        else:
            detail["blocksize"] = f.blocksize
        f.cache = MMapCache(f.blocksize, f._fetch_range, f.size, fn, blocks)
        close = f.close
        f.close = lambda: self.close_and_update(f, close)
        self.save_cache()
        return f

    def hash_name(self, path, same_name):
        return hash_name(path, same_name=same_name)

    def close_and_update(self, f, close):
        """Called when a file is closing, so store the set of blocks"""
        if f.closed:
            return
        path = self._strip_protocol(f.path)

        c = self.cached_files[-1][path]
        if c["blocks"] is not True and len(["blocks"]) * f.blocksize >= f.size:
            c["blocks"] = True
        try:
            logger.debug("going to save")
            self.save_cache()
            logger.debug("saved")
        except (IOError, OSError):
            logger.debug("Cache saving failed while closing file")
        except NameError:
            logger.debug("Cache save failed due to interpreter shutdown")
        close()
        f.closed = True

    def __getattribute__(self, item):
        if item in [
            "load_cache",
            "_open",
            "save_cache",
            "close_and_update",
            "__init__",
            "__getattribute__",
            "__reduce__",
            "_make_local_details",
            "open",
            "cat",
            "cat_file",
            "get",
            "read_block",
            "tail",
            "head",
            "_check_file",
            "_check_cache",
            "_mkcache",
            "clear_cache",
            "pop_from_cache",
            "_mkcache",
            "local_file",
            "_paths_from_path",
            "get_mapper",
            "open_many",
            "commit_many",
            "hash_name",
        ]:
            # all the methods defined in this class. Note `open` here, since
            # it calls `_open`, but is actually in superclass
            return lambda *args, **kw: getattr(type(self), item).__get__(self)(
                *args, **kw
            )
        if item in ["__reduce_ex__"]:
            raise AttributeError
        if item in ["_cache"]:
            # class attributes
            return getattr(type(self), item)
        if item == "__class__":
            return type(self)
        d = object.__getattribute__(self, "__dict__")
        fs = d.get("fs", None)  # fs is not immediately defined
        if item in d:
            return d[item]
        elif fs is not None:
            if item in fs.__dict__:
                # attribute of instance
                return fs.__dict__[item]
            # attributed belonging to the target filesystem
            cls = type(fs)
            m = getattr(cls, item)
            if (inspect.isfunction(m) or inspect.isdatadescriptor(m)) and (
                not hasattr(m, "__self__") or m.__self__ is None
            ):
                # instance method
                return m.__get__(fs, cls)
            return m  # class method or attribute
        else:
            # attributes of the superclass, while target is being set up
            return super().__getattribute__(item)


class WholeFileCacheFileSystem(CachingFileSystem):
    """Caches whole remote files on first access

    This class is intended as a layer over any other file system, and
    will make a local copy of each file accessed, so that all subsequent
    reads are local. This is similar to ``CachingFileSystem``, but without
    the block-wise functionality and so can work even when sparse files
    are not allowed. See its docstring for definition of the init
    arguments.

    The class still needs access to the remote store for listing files,
    and may refresh cached files.
    """

    protocol = "filecache"
    local_file = True

    def open_many(self, open_files):
        paths = [of.path for of in open_files]
        if "r" in open_files.mode:
            self._mkcache()
        else:
            return [
                LocalTempFile(self.fs, path, mode=open_files.mode, autocommit=False)
                for path in paths
            ]

        if self.compression:
            raise NotImplementedError
        details = [self._check_file(sp) for sp in paths]
        downpath = [p for p, d in zip(paths, details) if not d]
        downfn0 = [
            os.path.join(self.storage[-1], self.hash_name(p, self.same_names))
            for p, d in zip(paths, details)
        ]  # keep these path names for opening later
        downfn = [fn for fn, d in zip(downfn0, details) if not d]
        if downpath:
            # skip if all files are already cached and up to date
            self.fs.get(downpath, downfn)

            # update metadata - only happens when downloads are successful
            newdetail = [
                {
                    "fn": self.hash_name(path, self.same_names),
                    "blocks": True,
                    "time": time.time(),
                    "uid": self.fs.ukey(path),
                }
                for path in downpath
            ]
            self.cached_files[-1].update(
                {path: detail for path, detail in zip(downpath, newdetail)}
            )
            self.save_cache()

        def firstpart(fn):
            # helper to adapt both whole-file and simple-cache
            return fn[1] if isinstance(fn, tuple) else fn

        return [
            open(firstpart(fn0) if fn0 else fn1, mode=open_files.mode)
            for fn0, fn1 in zip(details, downfn0)
        ]

    def commit_many(self, open_files):
        self.fs.put([f.fn for f in open_files], [f.path for f in open_files])

    def _make_local_details(self, path):
        hash = self.hash_name(path, self.same_names)
        fn = os.path.join(self.storage[-1], hash)
        detail = {
            "fn": hash,
            "blocks": True,
            "time": time.time(),
            "uid": self.fs.ukey(path),
        }
        self.cached_files[-1][path] = detail
        logger.debug("Copying %s to local cache" % path)
        return fn

    def cat(self, path, recursive=False, on_error="raise", **kwargs):
        paths = self.expand_path(
            path, recursive=recursive, maxdepth=kwargs.get("maxdepth", None)
        )
        getpaths = []
        storepaths = []
        fns = []
        for p in paths:
            detail = self._check_file(p)
            if not detail:
                fn = self._make_local_details(p)
                getpaths.append(p)
                storepaths.append(fn)
            else:
                detail, fn = detail if isinstance(detail, tuple) else (None, detail)
            fns.append(fn)
        if getpaths:
            self.fs.get(getpaths, storepaths)
            self.save_cache()
        out = {path: open(fn, "rb").read() for path, fn in zip(paths, fns)}
        if isinstance(path, str) and len(paths) == 1 and recursive is False:
            out = out[paths[0]]
        return out

    def _open(self, path, mode="rb", **kwargs):
        path = self._strip_protocol(path)
        if "r" not in mode:
            return self.fs._open(path, mode=mode, **kwargs)
        detail = self._check_file(path)
        if detail:
            detail, fn = detail
            _, blocks = detail["fn"], detail["blocks"]
            if blocks is True:
                logger.debug("Opening local copy of %s" % path)
                return open(fn, mode)
            else:
                raise ValueError(
                    "Attempt to open partially cached file %s"
                    "as a wholly cached file" % path
                )
        else:
            fn = self._make_local_details(path)
        kwargs["mode"] = mode

        # call target filesystems open
        self._mkcache()
        if self.compression:
            with self.fs._open(path, **kwargs) as f, open(fn, "wb") as f2:
                if isinstance(f, AbstractBufferedFile):
                    # want no type of caching if just downloading whole thing
                    f.cache = BaseCache(0, f.cache.fetcher, f.size)
                comp = (
                    infer_compression(path)
                    if self.compression == "infer"
                    else self.compression
                )
                f = compr[comp](f, mode="rb")
                data = True
                while data:
                    block = getattr(f, "blocksize", 5 * 2 ** 20)
                    data = f.read(block)
                    f2.write(data)
        else:
            self.fs.get(path, fn)
        self.save_cache()
        return self._open(path, mode)


class SimpleCacheFileSystem(WholeFileCacheFileSystem):
    """Caches whole remote files on first access

    This class is intended as a layer over any other file system, and
    will make a local copy of each file accessed, so that all subsequent
    reads are local. This implementation only copies whole files, and
    does not keep any metadata about the download time or file details.
    It is therefore safer to use in multi-threaded/concurrent situations.

    This is the only of the caching filesystems that supports write: you will
    be given a real local open file, and upon close and commit, it will be
    uploaded to the target filesystem; the writability or the target URL is
    not checked until that time.

    """

    protocol = "simplecache"
    local_file = True

    def __init__(self, **kwargs):
        kw = kwargs.copy()
        for key in ["cache_check", "expiry_time", "check_files"]:
            kw[key] = False
        super().__init__(**kw)
        for storage in self.storage:
            if not os.path.exists(storage):
                os.makedirs(storage, exist_ok=True)
        self.cached_files = [{}]

    def _check_file(self, path):
        self._check_cache()
        sha = self.hash_name(path, self.same_names)
        for storage in self.storage:
            fn = os.path.join(storage, sha)
            if os.path.exists(fn):
                return fn

    def save_cache(self):
        pass

    def load_cache(self):
        pass

    def _open(self, path, mode="rb", **kwargs):
        path = self._strip_protocol(path)

        if "r" not in mode:
            return LocalTempFile(self, path, mode=mode)
        fn = self._check_file(path)
        if fn:
            return open(fn, mode)

        sha = self.hash_name(path, self.same_names)
        fn = os.path.join(self.storage[-1], sha)
        logger.debug("Copying %s to local cache" % path)
        kwargs["mode"] = mode

        self._mkcache()
        if self.compression:
            with self.fs._open(path, **kwargs) as f, open(fn, "wb") as f2:
                if isinstance(f, AbstractBufferedFile):
                    # want no type of caching if just downloading whole thing
                    f.cache = BaseCache(0, f.cache.fetcher, f.size)
                comp = (
                    infer_compression(path)
                    if self.compression == "infer"
                    else self.compression
                )
                f = compr[comp](f, mode="rb")
                data = True
                while data:
                    block = getattr(f, "blocksize", 5 * 2 ** 20)
                    data = f.read(block)
                    f2.write(data)
        else:
            self.fs.get(path, fn)
        return self._open(path, mode)


class LocalTempFile:
    """A temporary local file, which will be uploaded on commit"""

    def __init__(self, fs, path, fn=None, mode="wb", autocommit=True, seek=0):
        fn = fn or tempfile.mktemp()
        self.mode = mode
        self.fn = fn
        self.fh = open(fn, mode)
        if seek:
            self.fh.seek(seek)
        self.path = path
        self.fs = fs
        self.closed = False
        self.autocommit = autocommit

    def __reduce__(self):
        # always open in rb+ to allow continuing writing at a location
        return (
            LocalTempFile,
            (self.fs, self.path, self.fn, "rb+", self.autocommit, self.tell()),
        )

    def __enter__(self):
        return self.fh

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self.fh.close()
        self.closed = True
        if self.autocommit:
            self.commit()

    def discard(self):
        self.fh.close()
        os.remove(self.fn)

    def commit(self):
        self.fs.put(self.fn, self.path)

    def __getattr__(self, item):
        return getattr(self.fh, item)


def hash_name(path, same_name):
    if same_name:
        hash = os.path.basename(path)
    else:
        hash = hashlib.sha256(path.encode()).hexdigest()
    return hash
