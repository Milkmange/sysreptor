import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from datetime import date, datetime
from itertools import groupby
from typing import Any

from asgiref.sync import async_to_sync, sync_to_async
from django.db import close_old_connections, connections
from django.utils import dateparse, timezone
from django.utils.crypto import get_random_string
from randomcolor import RandomColor


def remove_duplicates(lst: list) -> list:
    return list(dict.fromkeys(lst))


def find_all_indices(s: str, find: str):
    idx = 0
    while True:
        idx = s.find(find, idx)
        if idx == -1:
            break
        else:
            yield idx
            idx += 1


def get_at(lst: list, idx: int, default=None):
    try:
        return lst[idx]
    except IndexError:
        return default


def find_index(lst: list, idx: int, default=-1):
    try:
        return lst.index(idx)
    except ValueError:
        return default


def get_key_or_attr(d: dict|object, k: str, default=None):
    return d.get(k, default) if isinstance(d, dict|OrderedDict) else getattr(d, k, default)


def set_key_or_attr(d: dict|object, k: str, value: Any):
    if isinstance(d, dict|OrderedDict):
        d[k] = value
    else:
        setattr(d, k, value)


def copy_keys(d: dict|object, keys: Iterable[str]) -> dict:
    keys = set(keys)
    out = {}
    for k in keys:
        if isinstance(d, dict|OrderedDict):
            if k in d:
                out[k] = d[k]
        else:
            if hasattr(d, k):
                out[k] = getattr(d, k)
    return out


def omit_keys(d: dict, keys: Iterable[str]) -> dict:
    keys = set(keys)
    return dict(filter(lambda t: t[0] not in keys, d.items()))


def omit_items(l: Iterable, items: Iterable) -> list:
    l = list(l)
    items = set(items)
    for i in items:
        while True:
            try:
                l.remove(i)
            except ValueError:
                break
    return l


def is_uuid(val) -> bool:
    try:
        uuid.UUID(val)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def is_json_string(val: str) -> bool:
    try:
        json.loads(val)
        return True
    except (TypeError, json.JSONDecodeError):
        return False


def is_date_string(val) -> bool:
    try:
        date.fromisoformat(val)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def is_unique(lst: Iterable) -> bool:
    lst = list(lst)
    return len(lst) == len(set(lst))


def parse_date_string(val: str):
    out = dateparse.parse_datetime(val)
    if out is None:
        raise ValueError()
    if not timezone.is_aware(out):
        out = timezone.make_aware(out)
    return out


def datetime_from_date(val: date) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return timezone.make_aware(datetime.combine(val, datetime.min.time()))
    raise ValueError(f'Expected date or datetime, got {type(val)}')


def merge(*args):
    """
    Recursively merge dicts
    """
    out = {}
    for d in args:
        if isinstance(d, dict|OrderedDict) and isinstance(out, dict|OrderedDict):
            for k, v in d.items():
                if k not in out:
                    out[k] = v
                else:
                    out[k] = merge(out.get(k), v)
        elif isinstance(d, list) and isinstance(out, list):
            l = []
            for i, dv in enumerate(d):
                if len(out) > i:
                    l.append(merge(out[i], dv))
                else:
                    l.append(dv)
            out = l
        else:
            out = d
    return out


def groupby_to_dict(data: dict, key) -> dict:
    return dict(map(lambda t: (t[0], list(t[1])), groupby(sorted(data, key=key), key=key)))


def get_random_color() -> str:
    return RandomColor(seed=get_random_string(8)).generate(luminosity='bright')[0]


_background_tasks = set()
def run_in_background(func):
    def inner(*args, **kwargs):
        def task_finished():
            if not connections['default'].in_atomic_block:
                close_old_connections()

        @sync_to_async(thread_sensitive=False)
        def wrapper():
            try:
                async_to_sync(func)(*args, **kwargs)
            except Exception:
                logging.exception(f'Error while running run_in_background({func.__name__})')
            finally:
                task_finished()

        task = asyncio.create_task(wrapper())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return inner
