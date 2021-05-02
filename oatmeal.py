#!/usr/bin/env python
"""
Oatmeal. A simple cookie manager for Chromium-based browser engines.

Copyright (C) 2021, Judd Vinet <jvinet@zeroflux.org>.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import sys
import os
from os.path import isfile
import sqlite3
import json
import re
import readline
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial, lru_cache

from rich import box
from rich.console import Console
from rich.table import Table
from rich.theme import Theme

__version__ = '0.1'

DB_PATH = './Cookies'
WL_PATH = './whitelist.json'
BL_PATH = './blacklist.json'

interactive = os.isatty(sys.stdin.fileno())

whitelist = []
blacklist = []

@dataclass()
class Selection:
    """
    This is the list of "selected" cookies that will be used or mutated by
    commands. The selection list is modified by the view commands (eg, 'ls').
    """
    data: list
    title: str
    page: int
    type: str

    def set(self, data, title, page=0, type='cookies'):
        self.data = data
        self.title = title
        self.page = page
        self.type = type  # cookies/whitelist/blacklist

selection = Selection(data=None, title=None, page=0, type=None)

theme = Theme({'info': 'deep_sky_blue1',
               'action': 'green',
               'key': 'pale_turquoise4',
               'value': 'cyan3',
               'error': 'bold red',
               'warning': 'yellow',
               'hl': 'dark_slate_gray1',
               'repr.str': 'bold deep_sky_blue1',
               'repr.tag_name': 'bold yellow',
               'repr.number': 'bold yellow',
               'table.row1': 'cyan',
               'table.row2': 'pale_turquoise4'})
console = Console(theme=theme)


#-----------------------------------------------------------------------------
# COMMANDS
#-----------------------------------------------------------------------------

def cmd_help(command=None, *args):
    """
    Provide usage documentation for commands.

    Usage: h [[repr.tag_name]command[/repr.tag_name]]
    """
    if command:
        if command not in COMMANDS:
            err("Unknown command. Try 'h' for help.")
            return 1
        func = COMMANDS[command]
        if not getattr(func, '__doc__'):
            err(f'No help available for [pink1]{command}[/pink1]')
            return 0
        info(re.sub(r'Usage: (\w+)', 'Usage: [hl]\\1[/hl]', func.__doc__))
        return 0

    # List all commands.
    table = Table.grid(padding=(0, 4, 0, 0))
    table.add_column(min_width=10, style='info')
    table.add_column(style='white')
    for cmd, func in COMMANDS.items():
        # We have sub-headers packed into the command list, identified by
        # leading octothorpes.
        if cmd.startswith('#'):
            table.add_row(f'  [yellow]{func}[/yellow]', style='bold')
            continue
        # We also have command aliases, which do not appear in the help.
        if cmd.startswith('>'):
            continue
        if getattr(func, '__doc__'):
            doc = func.__doc__.strip().split('\n')[0]
        else:
            doc = ''
        table.add_row(f'    {cmd}', doc)

    console.print('[bold]Commands:[/bold]')
    console.print(table)
    return 0


def cmd_quit(*args):
    """
    Save black/white lists and exit.

    Usage: q
    """
    shutdown()


def cmd_exit(*args):
    """
    Exit without saving black/white lists.

    Usage: x
    """
    shutdown(save=False)


def cmd_list(*args):
    """
    List the current page of the selection.
    """
    if selection.data is None:
        return warn("No selection.")
    if selection.type == 'cookies':
        return cookie_list()
    return bw_list()


def cmd_count(host=None, *args):
    """
    Count all entries in the selection.

    Usage: c
    """
    ct = len(selection.data)
    s = 'cookies' if selection.type == 'cookies' else f'{selection.type} entries'
    info(f"There are [yellow]{ct}[/yellow] {s}.")


def cmd_next_page(*args):
    """
    Next Page.

    Usage: n
    """
    if (selection.page + 1) * page_size() >= len(selection.data):
        warn("Last page reached.")
        return 0
    selection.page += 1
    if interactive:
        return cmd_list()


def cmd_prev_page(*args):
    """
    Previous page.

    Usage: p
    """
    if selection.page == 0:
        warn("First page reached.")
        return 0
    selection.page -= 1
    if interactive:
        return cmd_list()


def cmd_view(num=None, *args):
    """
    View details for a cookie, referenced by number.

    Usage: v <num>
    """
    if not num:
        return err_arg('num')
    try:
        num = int(num)
    except:
        return err('Invalid number.')
    if num < 1 or num > len(selection.data):
        return err('Number exceeds the selection range.')

    if selection.type != 'cookies':
        warn("This command only works on cookie selections. "
             "Try using 'sca' or 'sch' first.")
        return 1

    # Our list is zero-indexed, but the numbers passed into the command are
    # one-indexed, so we account for that.
    c = selection.data[num - 1]

    table = Table.grid()
    table.add_column(min_width=14, style='key')
    table.add_column(style='value')
    table.add_row('Host:', c['host_key'])
    table.add_row('Name:', c['name'])
    table.add_row('Path:', c['path'])
    table.add_row('Created:', ts_to_datetime(c['creation_utc']))
    table.add_row('Last Used:', ts_to_datetime(c['last_access_utc']))
    table.add_row('Expires:', ts_to_datetime(c['expires_utc']))
    table.add_row('Secure:', str(c['is_secure']))
    table.add_row('HTTP Only:', str(c['is_httponly']))
    table.add_row('Persistent:', str(c['is_persistent']))
    table.add_row('Priority:', str(c['priority']))
    table.add_row('Same Site:', str(c['samesite']))
    table.add_row('Value:', str(c['value']))

    console.print(table)


def cmd_select_cookies_all(*args):
    """
    Select all cookies in DB.

    Usage: sca
    """
    cookies = cookie_find()
    selection.set(cookies, 'All Cookies')


def cmd_select_cookies_by_host(host=None, *args):
    """
    Select all cookies matching host.

    Usage: sch <host>
    """
    if not host:
        return err_arg('host')

    cookies = cookie_find('host_key LIKE ?', (f'%{host}%',))
    selection.set(cookies, f"Cookies for '{host}'")


def cmd_select_blacklist_all(*args):
    """
    Select all entries in blacklist.

    Usage: sba
    """
    selection.set(blacklist, 'Full Blacklist', type='blacklist')


def cmd_select_blacklist_by_host(host=None, *args):
    """
    Select all blacklist entries matching host.

    Usage: sbh <host>
    """
    if not host:
        return err_arg('host')

    # Look for substring matches.
    matches = [x for x in blacklist if host in x]
    selection.set(matches, f"Blacklist Entries for '{host}'", type='blacklist')


def cmd_select_whitelist_all(*args):
    """
    Select all entries in whitelist.

    Usage: swa
    """
    selection.set(whitelist, 'Full whitelist', type='whitelist')


def cmd_select_whitelist_by_host(host=None, *args):
    """
    Select all whitelist entries matching host.

    Usage: swh <host>
    """
    if not host:
        return err_arg('host')

    # Look for substring matches.
    matches = [x for x in whitelist if host in x]
    selection.set(matches, f"Whitelist Entries for '{host}'", type='whitelist')


def cmd_add_host(host=None, *args):
    """
    Add a host to the selection.

    Usage: ah
    """
    if not host:
        return err_arg('host')
    if selection.type == 'cookies':
        warn("This command only works on the blacklist or whitelist. "
             "Select the blacklist ('sba') or whitelist ('swa') first.")
        return 1

    lst = {'blacklist': blacklist, 'whitelist': whitelist}[selection.type]
    if host in lst:
        err(f"Host '{host}' is already in {selection.type}.")
        return 1

    lst.append(host)
    info(f"Added '{host}' to {selection.type}.")


def cmd_delete_by_number(number_range=None, *args):
    """
    Delete one or more entries by number.

    Usage: dn <number_range>

    Examples:
      dn 3
      dn 6-12
      dn 6,9,3,11,14-17
    """
    if not number_range:
        return err_arg('number_range')

    idxs = []
    # Validate all specifiers before deleting a single record.
    specs = number_range.split(',')
    for spec in specs:
        if not re.match('^[0-9-]+$', spec):
            return err("Argument 'number_range' contains invalid characters.")

        if re.match('^[0-9]+-[0-9]+$', spec):
            start, end = map(int, spec.split('-'))
            if start < 1 or end > len(selection.data):
                return err(f"Index '{spec}' is out of range")
            idxs.extend(list(range(start, end+1)))
        elif re.match('^[0-9]+$', spec):
            spec = int(spec)
            if spec < 1 or spec > len(selection.data):
                return err(f"Index '{spec}' is out of range")
            idxs.extend([spec])
        else:
            return err(f"Number range invalid or unsupported: '{spec}'")

    # Remove them from the DB.
    if selection.type == 'cookies':
        to_del = [selection.data[i - 1] for i in idxs]
        cookie_delete(to_del)

    # Remove them from the selection.
    ct = 0
    for idx in sorted(idxs, reverse=True):
        # Our list is zero-indexed, but the ranges passed into the command are
        # one-indexed, so we account for that.
        idx -= 1

        c = selection.data[idx]
        if selection.type == 'cookies':
            act(f"Deleting: {cookie_repr(c)}")
            del selection.data[idx]
        else:
            act(f"Deleting: {bw_repr(c)}")
            del selection.data[idx]

        ct += 1

    s = f'entries from ' if selection.type != 'cookies' else ''
    info(f"Deleted [yellow]{ct}[/yellow] {s}{selection.type}.")


def cmd_delete_all(*args):
    """
    Delete all entries in selection.

    If the selection target is the cookie list, then cookies whose host matches
    a whitelist entry will not be deleted.

    Usage: da
    """
    idxs = None

    # Remove them from the DB.
    if selection.type == 'cookies':
        idxs =  []
        for idx, c in enumerate(selection.data):
            # Check the whitelist to see if this guy gets a pass.
            if any(host_match(c['host_key'], w) for w in whitelist):
                info(f"Cookie matches whitelist, ignoring: {cookie_repr(c)}")
            else:
                idxs.append(idx)
        cookie_delete([c for i, c in enumerate(selection.data) if i in idxs])

    # Remove them from the selection.
    if idxs is None:
        ct = len(selection.data)
        selection.data[:] = []
    else:
        ct = len(idxs)
        for idx in sorted(idxs, reverse=True):
            del selection.data[idx]

    s = f'entries from ' if selection.type != 'cookies' else ''
    info(f"Deleted [yellow]{ct}[/yellow] {s}{selection.type}.")


def cmd_delete_by_expired(*args):
    """
    Delete expired cookies from the selection.

    Usage: de
    """
    if selection.type != 'cookies':
        warn("This command only works on cookie selections. "
             "Try using 'sca' or 'sch' first.")
        return 1

    ct = 0
    ts = datetime_to_ts(datetime.utcnow())
    idxs = [i for i, c in enumerate(selection.data) if c['expires_utc'] < ts]

    # Remove them from the DB.
    if selection.type == 'cookies':
        to_del = [selection.data[i] for i in idxs]
        cookie_delete(to_del)

    # Remove them from the selection.
    for idx in sorted(idxs, reverse=True):
        del selection.data[idx]
        ct += 1

    info(f"Deleted [yellow]{ct}[/yellow] cookies")


def cmd_delete_by_blacklist(*args):
    """
    Delete all selected cookies whose hosts are found in the blacklist.
    Cookies whose host matches a whitelist entry will not be deleted.

    Usage: db
    """
    if selection.type != 'cookies':
        warn("This command only works on cookie selections. "
             "Try using 'sca' or 'sch' first.")
        return 1

    idxs = []
    for bl_host in blacklist:
        for idx, c in enumerate(selection.data):
            # We'll be passing these indexes to the `cmd_delete_by_number()`
            # command, so these numbers need to be one-indexed.
            idx += 1

            if host_match(c['host_key'], bl_host):
                # Check the whitelist to see if this guy gets a pass.
                if any(host_match(c['host_key'], w) for w in whitelist):
                    info(f"Cookie matches whitelist, ignoring: {cookie_repr(c)}")
                else:
                    idxs.append(idx)

    if idxs:
        return cmd_delete_by_number(','.join(map(str, idxs)))


COMMANDS = {'#1': 'Select',
            'sca': cmd_select_cookies_all,
            'sch': cmd_select_cookies_by_host,
            'sba': cmd_select_blacklist_all,
            'sbh': cmd_select_blacklist_by_host,
            'swa': cmd_select_whitelist_all,
            'swh': cmd_select_whitelist_by_host,

            '#2': 'List',
            'l': cmd_list,
            '>ls': 'l',          # Muscle memory. ¯\_(ツ)_/¯
            'c': cmd_count,
            'n': cmd_next_page,
            'p': cmd_prev_page,

            '#3': 'View',
            'v': cmd_view,

            '#4': 'Add',
            'ah': cmd_add_host,

            '#5': 'Delete',
            'dn': cmd_delete_by_number,
            'da': cmd_delete_all,
            'de': cmd_delete_by_expired,
            'db': cmd_delete_by_blacklist,

            '#6': 'Other',
            'h': cmd_help,
            'x': cmd_exit,
            'q': cmd_quit}


#-----------------------------------------------------------------------------
# BW-LIST MGMT ROUTINES
#-----------------------------------------------------------------------------


def bw_list():
    table = Table(title=selection.title)
    table.add_column('#', style='green', justify='right', no_wrap=True)
    table.add_column('Host', style='cyan3', no_wrap=True)

    idx = 1
    for host in selection.data[:page_size()]:
        table.add_row(str(idx), host)
        idx += 1

    console.print(table)
    return 0


def bw_repr(host):
    """ Return a pretty string-representation of a bw-list entry. """
    return f"[key]host=[/key][value]{host}[/value]"


#-----------------------------------------------------------------------------
# COOKIE MGMT ROUTINES
#-----------------------------------------------------------------------------


def cookie_delete(cookies):
    if not isinstance(cookies, (list, tuple)):
        cookies = [cookies]

    con = sqlite3.connect(DB_PATH)
    for c in cookies:
        con.execute("DELETE FROM cookies WHERE host_key=? AND name=? AND path=?",
                    (c['host_key'], c['name'], c['path']))
    con.commit()
    rc = con.total_changes
    con.close()


def cookie_find(where='1=1', params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    rv = []
    q = f'SELECT * FROM cookies WHERE {where} ORDER BY creation_utc DESC'
    for row in con.execute(q, params):
        rv.append({k: row[k] for k in row.keys()})

    con.close()
    return rv


def cookie_list():
    table = Table(title=selection.title)
    table.add_column('#', style='green', justify='right', no_wrap=True)
    table.add_column('Host', style='cyan3', no_wrap=True)
    table.add_column('Name')
    table.add_column('Path')
    table.add_column('Created', justify='right', no_wrap=True)
    table.add_column('Expires', justify='right', no_wrap=True)

    pg_sz = page_size()
    start = selection.page * pg_sz
    end = start + pg_sz

    idx = start + 1
    for c in selection.data[start:end]:
        if c['expires_utc']:
            expires = ts_to_datetime(c['expires_utc'])
        else:
            expires = 'none'

        # Shouldn't ever be null, but you never know...
        if c['creation_utc']:
            created = ts_to_datetime(c['creation_utc'])
        else:
            created = 'none'

        s = 'table.row1' if idx % 2 else 'table.row2'
        table.add_row(str(idx), c['host_key'], c['name'], c['path'], created,
                      expires, style=s)
        idx += 1

    console.print(table)


def cookie_repr(c):
    """ Return a pretty string-representation of a cookie. """
    return f"[key]host=[/key][cyan3]{c['host_key']}[/cyan3] " +\
           f"[key]name=[/key][cyan3]{c['name']}[/cyan3] " +\
           f"[key]path=[/key][cyan3]{c['path']}[/cyan3]"


#-----------------------------------------------------------------------------
# MAIN
#-----------------------------------------------------------------------------


def shutdown(save=True):
    if save:
        save_json_file(WL_PATH, whitelist)
        save_json_file(BL_PATH, blacklist)
    # Reclaim deleted space in the DB file.
    con = sqlite3.connect(DB_PATH)
    con.execute("VACUUM")
    con.close()
    sys.exit(0)


def handle_statement(inp):
    inp = inp.strip().split(' ')
    cmd, args = inp[0].strip(), inp[1:]

    if not cmd:
        return 0

    if cmd.startswith(('#', '>')) or cmd not in COMMANDS:
        # Check if it's an alias before rejecting it.
        if f'>{cmd}' in COMMANDS:
            cmd = COMMANDS[f'>{cmd}']
        else:
            err("Unknown command. Try 'h' for help.")
            return 0

    return COMMANDS[cmd](*args) or 0


def process_input(inp):
    # Multiple statements can be specified if they are delimited by semicolons,
    # similar to a real shell.
    for stmt in inp.split(';'):
        rv = handle_statement(stmt)
        # A negative return value indicates that we should abort.
        if rv < 0:
            return rv
    return 0


def loop():
    try:
        inp = console.input('> ')
    except EOFError:
        console.print('q')
        # Tell `main()` to exit.
        return -1
    return process_input(inp)


def main(argv):
    p = argparse.ArgumentParser(description='Manage cookies for QT WebEngine')
    p.add_argument('-c', metavar='PATH', help='location of cookie database file')
    p.add_argument('-b', metavar='PATH', help='location of blacklist file')
    p.add_argument('-w', metavar='PATH', help='location of whitelist file')
    p.add_argument('-e', metavar='CMD',
                   help='execute these commands and exit ' +
                        '(separate multiple commands with semicolons)')
    p.add_argument('-v', action='version', version=f'Oatmeal {__version__}')
    args = p.parse_args(argv[1:])

    if args.c:
        global DB_PATH
        DB_PATH = args.c
    if args.b:
        global BL_PATH
        BL_PATH = args.b
    if args.w:
        global WL_PATH
        WL_PATH = args.w

    # Load whitelist and blacklist.
    global whitelist, blacklist
    try:
        whitelist = load_json_file(WL_PATH)
        blacklist = load_json_file(BL_PATH)
    except:
        sys.exit(1)

    # Ensure the database exists and is actually a database.
    if not isfile(DB_PATH):
        err(f"Database file '{DB_PATH}' not found")
        sys.exit(1)
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("SELECT * FROM cookies LIMIT 1")
        con.close()
    except sqlite3.DatabaseError as e:
        err("Database error: %s" % e)
        sys.exit(1)

    # If `-e` was used, then we are not in interactive mode. Run the specified
    # commands and bail.
    if args.e:
        global interactive
        interactive = False
        process_input(args.e)
        shutdown()

    # Set & display the initial selection.
    cmd_select_cookies_all()
    cmd_list()

    while True:
        if loop() < 0:
            shutdown()

#-----------------------------------------------------------------------------
# HELPERS
#-----------------------------------------------------------------------------


def load_json_file(path):
    try:
        with open(path, 'r') as f:
            return json.loads(f.read())
    except ValueError:
        err(f"Failed to load '{path}', invalid JSON")
        raise
    except FileNotFoundError:
        return []
    except Exception as e:
        err(f"Failed to read '{path}': {e}")
        raise


def save_json_file(path, body):
    try:
        with open(path, 'w') as f:
            f.write(json.dumps(body))
    except ValueError:
        err(f"Failed to serialize data to JSON")
        raise
    except Exception as e:
        err(f"Failed to write to '{path}': {e}")
        raise
    return None


@lru_cache(maxsize=4096)
def ts_to_datetime(ts):
    # For some presumably-genius reason, the timestamp epoch starts at
    # 1601-01-01T00:00:00Z. And it's in microseconds. You know, for those
    # really high-precision cookies that are necessary to track your site
    # visits down to the millionth of a second since the WWW was first
    # invented in the early 1600s.
    #
    # https://stackoverflow.com/questions/43518199/cookies-expiration-time-format
    ts = ts // 1e6 - 11644473600
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%SZ')


@lru_cache(maxsize=4096)
def datetime_to_ts(dt):
    ts = dt.replace(tzinfo=timezone.utc).timestamp()
    # Now transmogrify it into this weirdo version.
    return (ts + 11644473600) * 1e6


@lru_cache(maxsize=4096)
def host_match(c_host, bw_host):
    """ Check if a cookie `c_host` matches a bw-list `bw_host`. """
    if c_host == bw_host:
        return True
    elif bw_host.startswith('.') and c_host.endswith(bw_host):
        return True
    return False


def page_size():
    # Keep only what we can fit on the screen. Leave six rows: three for the
    # table header, one for the title, one for the footer, and one for our
    # command prompt.
    return Console().size.height - 6


def info(msg):
    """ Generate an info message. """
    console.print(msg, style='info')


def act(msg):
    """ Generate a status/action message. """
    console.print(msg, style='action')


def warn(msg):
    """ Generate a warning message. """
    console.print(msg, style='warning')
    return 1


def err(msg):
    """ Generate an error message. """
    console.print(msg, style='error')
    return 1


def err_arg(field):
    """ Generate an error for a missing required argument. """
    return err("Missing required argument: [hl]%s[/hl]" % field)


if __name__ == '__main__':
    main(sys.argv)
