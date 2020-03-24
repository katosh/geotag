import os
import pickle
import argparse
import pandas as pd
import numpy as np
import curses
from curses.textpad import Textbox, rectangle
import time
import locale
import logging
import yaml
from .undo import stack, undoable
from pydoc import locate

# use system default localization
locale.setlocale(locale.LC_ALL, 'C')
code = locale.getpreferredencoding()

def main():
    desc = 'Interface to quickly tag geo data sets.'
    parser = argparse.ArgumentParser(description=desc,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--rnaSeq',
                        help='.RDS table containing the extraction status of '
                        'RNASeq studies.', type=str, metavar='path',
                        #default='/mnt/ribolution/user_worktmp/dominik.otto/'
                        #'tumor-deconvolution-dream-challenge/'
                        #'extraction_stats_head.tsv')
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        "geotag/data/extraction_stats.tsv")
    parser.add_argument('--array',
                        help='.RDS table containing the extraction status of '
                        'microarray studies.', type=str, metavar='path',
                        #default='/mnt/ribolution/user_worktmp/dominik.otto/'
                        #'tumor-deconvolution-dream-challenge/'
                        #'extraction_stats_array.tsv')
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        "geotag/data/extraction_stats_array.tsv")
    parser.add_argument('--user',
                        help='The user name under which to write tags and log.',
                        type=str, metavar='name',
                        default=os.environ['USER'])
    parser.add_argument('--log',
                        help='The file path for the log.',
                        type=str, metavar='path',
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        f"geotag/data/{os.environ['USER']}.log")
    parser.add_argument('--tags',
                        help='The file path for the tag yaml.',
                        type=str, metavar='path.yml',
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        f"geotag/data/tags.yaml")
    parser.add_argument('--output',
                        help='The output file path.',
                        type=str, metavar='path.yml',
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        f"geotag/data/{os.environ['USER']}.yml")
    parser.add_argument('--softPath',
                        help='Path to the soft file directory.',
                        type=str, metavar='path.yml',
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        f"geotag/data/{os.environ['USER']}.yml")
    parser.add_argument('--state',
                        help='Path to a cached state of geotag.',
                        type=str, metavar='path.pkl',
                        default="/home/dominik/rn_home/dominik.otto/Projects/"
                        f"geotag/data/{os.environ['USER']}.pkl")
    parser.add_argument('--update',
                        help='Overwrite the cache.',
                        action="store_true")
    args = parser.parse_args()
    if not os.environ.get('TMUX'):
        raise Exception('Please run geotag inside a tmux.')
    app = App(**vars(args))
    if not args.update and os.path.exists(args.state):
        try:
            with open(args.state, 'rb') as f:
                app.cache = pickle.load(f)
        except Exception as e:
            print('Failed to load previos state. Pass --update to overwrite.')
            raise
    try:
        print('Starting curses app ...')
        curses.wrapper(app.run)
    finally:
        print('Saving last state ...')
        with open(args.state, 'wb') as f:
            pickle.dump(app.cache, f)

tcolors = [196, 203, 202, 208, 178, 148, 106, 71, 31, 26]
tag_characteristics = ['key', 'type', 'col_width', 'desc']
default_tags = {
    'quality':{
        'type':'int',
        'desc':'From 0 to 9.',
        'key':'q',
        'col_width':8
    },
    'note':{
        'type':'str',
        'desc':'A note.',
        'key':'n',
        'col_width':20
    }
}

def uniquify(vals):
    seen = set()
    for item in vals:
        fudge = 1
        newitem = item
        while newitem in seen:
            fudge += 1
            newitem = "{}_{}".format(item, fudge)
        yield newitem
        seen.add(newitem)

def keypartmap(c):
    if c == 27:
        return 'Alt'
    else:
        return chr(c)

class App:
    __version__ = '0.0.1'

    def __init__(self, rnaSeq, array, log, tags, output, user, softPath,
                 **kwargs):
        logging.basicConfig(filename=log, filemode='a', level=logging.DEBUG,
                format='[%(asctime)s] %(levelname)s: %(message)s')
        self.log = log
        self.user = user
        self.array = array
        self.rnaSeq = rnaSeq
        self.output = output
        self.softPath = softPath
        self.tags_file = tags
        self.error = None
        print('Loading data ...')
        rnaSeq_df = pd.read_csv(self.rnaSeq, sep="\t", low_memory=False)
        array_df = pd.read_csv(self.array, sep="\t", low_memory=False)
        self.raw_df = pd.concat([rnaSeq_df, array_df])
        self.raw_df.index = uniquify(self.raw_df['id'])
        smap_counts = self.raw_df['gse'].value_counts()
        self.raw_df['n_sample'] = smap_counts[self.raw_df['gse']].values
        self._measured_col_width = dict()
        for col in self.raw_df.columns:
            l = self.raw_df[col].astype(str).map(len).quantile(.99)
            self._measured_col_width[col] = int(max(l, len(col)))
        self.sort_columns = set()
        self.sort_reverse_columns = set()
        self.column_seperator = ' '
        self.filter = dict()
        self.toggl_help(False)
        self.in_dialog = False
        self.in_tag_dialog = False
        self.pointer = 0
        self.col_pointer = 0
        self.tag_pointer = 0
        self.tag_error = None
        self.serious = False
        self.add_tag = False
        self.selection = {self.pointer}
        self.lrpos = 0
        self.top = 0
        self.last_saver_pid = None
        self.color_by = 'quality'
        self.current_tag = 'quality'
        self.sort_reverse_columns.add('n_sample')
        self.tags = dict()
        self.tag_data = dict()
        if not os.path.exists(self.output):
            logging.warning(f'The output file "{self.output}" dose not exist '
                            'yet. Starting over...')
            self.tag_data = dict()
        else:
            with open(self.output, 'rb') as f:
                data = yaml.load(f, Loader=yaml.SafeLoader)
            if not isinstance(data, dict):
                logging.warning(f'The loaded {self.output} is no dict '
                                'and will be resetted.')
                self.data = dict()
            else:
                self.data = data
        self.load_tag_definitions()
        self.reset_cols()
        self._update_now = True

    def load_tag_definitions(self):
        try:
            with open(self.tags_file, 'rb') as f:
                self.tags.update(yaml.load(f, Loader=yaml.SafeLoader))
        except IOError:
            pass
        if not self.tags:
            logging.warning(f'The tags file "{self.tags}" could not be read. '
                           'Using default tags...')
            self.tags = default_tags
        for tag in self.tags:
            self.tag_data.setdefault(tag, dict())

    def save_tag_definitions(self):
        temp_out = self.tags_file+'.'+self.user
        with open(temp_out, 'w') as f:
            f.write(yaml.dump(self.tags))
        os.rename(temp_out, self.tags_file)

    def col_widths(self):
        for col in self.df.columns:
            if col in self._measured_col_width:
                yield col, self._measured_col_width[col]
            elif col in self.tags:
                yield col, self.tags[col]['col_width']

    def reset_cols(self):
        ordered_columns = ['id']
        ordered_columns += list(self.tags.keys())
        ordered_columns += [
            'gse',
            'n_sample',
            'technology',
            'status',
            'coarse.cell.type',
            'fine.cell.type',
            'pattern',
            'col',
            'val'
        ]
        all_columns = set(self.raw_df.columns).union(set(self.tags.keys()))
        ordered_columns = [c for c in ordered_columns if c in all_columns]
        self.show_columns = set(ordered_columns)
        for c in all_columns:
            if c not in ordered_columns:
                ordered_columns.append(c)
        self.ordered_columns = ordered_columns

    def toggl_help(self, to: bool=None):
        if to is not None:
            self.print_help = to
        else:
            self.print_help = not self.print_help

    def update_df(self):
        r = self.raw_df.copy()
        data_frames = [self.raw_df.copy()]
        for col, tags in self.tag_data.items():
            tagd = pd.DataFrame.from_dict(tags, orient='index', columns=[col],
                                          dtype=locate(self.tags[col]['type']))
            data_frames.append(tagd)
        r = pd.concat(data_frames, axis=1, join='outer').fillna('-')
        for col, filter in self.filter.items():
            r = r[r[col].astype(str).str.contains(filter)]
        if self.sort_columns or self.sort_reverse_columns:
            sort_cols = self.sort_columns.union(self.sort_reverse_columns)
            sc = [c for c in self.ordered_columns if c in sort_cols]
            ascending = [True if c in self.sort_columns else False for c in sc]
            r = r.sort_values(sc, ascending=ascending)
        cols = [c for c in self.ordered_columns if c in self.show_columns]
        r = r[cols]
        if r.empty:
            logging.debug('No entries match the filter.')
            r = pd.DataFrame({
                'id':['none'],
                'gse':['None']
            })
        self.df = r
        if self.color_by not in r.columns:
            self.coloring_now = False
            return
        self.coloring_now = self.color_by
        tag_type = self.tags.get(self.color_by, dict()).get('type', '')
        if tag_type == 'int' or \
                pd.api.types.is_numeric_dtype(r[self.color_by].dtype):
            self.colmap = lambda x: 99 if x=='-' else int(x%10)+1
        else:
            cmap = {key:i%10+1 for i, key in
                    enumerate(sorted(set(r[self.color_by])-{None}))}
            self.colmap = cmap.get

    def _init_curses(self):
        curses.use_default_colors()
        curses.init_pair(99, -1, -1)
        curses.init_pair(100, curses.COLOR_GREEN, -1)
        curses.init_pair(101, curses.COLOR_BLUE, -1)
        curses.init_pair(102, curses.COLOR_RED, -1)
        curses.init_pair(103, curses.COLOR_WHITE, -1)
        curses.init_pair(104, curses.COLOR_YELLOW, -1)
        curses.init_pair(105, curses.COLOR_CYAN, -1)
        for i, col in enumerate(tcolors):
            curses.init_pair(i+1, col, -1)

    def _format(self, val, col, width):
        if val=='-':
            return ' '*(width-1)+'-'
        tag_dtype = self.tags.get(col, dict()).get('type', '')
        if tag_dtype == 'int':
            text = f'%{width}d' % val
        else:
            text = str(val).splitlines()[0]
        if len(text)>width:
            return text[:(width-3)]+'...'
        return text.rjust(width)

    def _str_from_line(self, line=None):
        """ Returns string for line list and  header line if line==None. """
        if line is None:
            return self.column_seperator.join(
                self._format(col, None, w) for col, w in self.col_widths()
            )
        return self.column_seperator.join(
            self._format(l, col, w) for l, (col, w) in
                zip(line, self.col_widths())
        )

    def update_content(self):
        self.update_df()
        one_line = self.df.iloc[:1, :]
        self.header = self._str_from_line()
        self.total_lines = self.df.shape[0]
        self.lines = list(range(self.total_lines))
        self.stale_lines = set(range(self.total_lines))
        if self.pointer > self.total_lines:
            self.pointer = 0
            self.selection = {self.pointer}

    def update_lines(self, line_numbers):
        locs = set(line_numbers).intersection(self.stale_lines)
        if not locs:
            return
        ordered_locs = list(locs)
        logging.debug(self.df.columns)
        for j in ordered_locs:
            self.lines[j] = self._str_from_line(self.df.iloc[j, :])
        self.stale_lines -= locs

    def run(self, stdscr):
        self._init_curses()
        self.stdscr = stdscr
        cn = None
        stdscr.addstr('Loading visualization ...')
        stdscr.refresh()
        while True:
            if self._update_now:
                self.update_content()
                self._update_now = False
            curses.update_lines_cols()
            padding = ' '*curses.COLS
            nlines = curses.LINES-4
            if self.pointer > self.total_lines-1:
                logging.debug('Resetting pointer to 0.')
                self.pointer = 0
            self.top = self.pointer if self.pointer < self.top else self.top
            self.top = self.pointer - nlines \
                    if self.pointer >= self.top + nlines else self.top
            tabcols = curses.COLS
            cols = slice(self.lrpos, self.lrpos+tabcols)
            button = self.top + nlines
            viewed_lines = range(self.top, button+1)
            self.update_lines(viewed_lines)
            self._print_body(self.header, self.lines, nlines, cols)
            curses.setsyx(nlines+2, 0)
            if len(self.selection) == 1:
                sel_status = self._id_for_index(self.pointer)
            else:
                sel_status = str(len(self.selection))
            status_bar = [
                ('help', 'h', 100),
                ('tagging', self.current_tag, 100),
                ('selected', sel_status, 100),
            ]
            if cn:
                status_bar.append(('key', str(cn), 100))
            if stack().canundo():
                status_bar.append(('undoable', stack().undotext(), 100))
            if stack().canredo():
                status_bar.append(('redoable', stack().redotext(), 100))
            if self.error:
                status_bar = [('error', self.error, 102)] + status_bar
                self.error = None
            for name, content, color in status_bar:
                y, x = stdscr.getyx()
                name = f' {name}: '
                space = curses.COLS-1-x
                if y==curses.LINES-1 and len(name)+len(content)>space:
                    stdscr.addstr(' ...'[:space])
                    break
                stdscr.addstr(name, curses.color_pair(color))
                stdscr.addstr(content)
            y, x = stdscr.getyx()
            stdscr.addstr(' '*(curses.COLS-x-1))
            if y < curses.LINES-1:
                # clear last line
                stdscr.addstr(' '*(curses.COLS-1))
            if self.print_help:
                self._print_help()
            if self.in_dialog:
                self._view_dialoge()
            elif self.in_tag_dialog:
                self._view_tag_dialoge()
            if not self.add_tag:
                cn = self.get_key(stdscr)
            else:
                cn = b''
                self.add_tag = False
            if cn == b'q':
                break
            if self.in_dialog:
                self._dialog(cn)
            elif self.in_tag_dialog:
                self._tag_dialog(cn)
            else:
                self._react(cn, nlines, tabcols)

    _byte_numbers = {str(i).encode() for i in range(10)}
    _control_seq_parts = _byte_numbers.copy()
    _control_seq_parts.add(b';')
    _control_seq_parts.add(b'[')
    _required_columns = {'id', 'gse'}

    def get_key(self, win):
        def get():
            try:
                return win.get_wch()
            except Exception as e:
                if str(e) != 'no input':
                    raise
                return ''
        c = get()
        try:
            cn = curses.keyname(c)
            return cn
        except TypeError:
            pass
        cn = c.encode()
        if cn != b'\x1b':
            return cn
        next_c = str(get()).encode()
        if next_c == b'[':
            # this is a Control Sequence Introducer
            while next_c in self._control_seq_parts:
                cn += next_c
                next_c = get().encode()
        return cn + next_c

    def _print_body(self, header, lines, nlines, cols, y0=0, x0=0):
        padding = ' '*curses.COLS
        h = header + padding
        self.stdscr.addstr(y0, x0, h[cols])
        for i in range(nlines+1):
            pos = i + self.top
            attr = curses.A_REVERSE if self.is_selected(pos) else curses.A_NORMAL
            if pos >= self.total_lines:
                self.stdscr.addstr(y0+i+1, 0, padding)
                continue
            if self.coloring_now in self.df.columns:
                val = self.df[self.coloring_now].iloc[pos]
                if val is not None:
                    col = self.colmap(val)
                    attr |= curses.color_pair(col)
            text = lines[pos]+padding
            self.stdscr.addstr(y0+i+1, x0, text[cols], attr)

    def is_selected(self, pointer):
        return pointer in self.selection

    def _react(self, cn, nlines, tabcols):
        if cn == b'\x01': # CTRL + a
            self.selection = set(range(self.total_lines))
        elif cn == b'h':
            self.toggl_help()
        elif cn == b'f':
            self._dialog_changed = False
            self.in_dialog = True
        elif cn == b't':
            self._dialog_changed = False
            self.in_tag_dialog = True
        elif cn == b'KEY_UP':
            self.pointer -= 1
            self.pointer %= self.total_lines
            self.selection = {self.pointer}
        elif cn == b'KEY_DOWN':
            self.pointer += 1
            self.pointer %= self.total_lines
            self.selection = {self.pointer}
        elif cn == b'KEY_SR' or cn == b'\x1b[1;2A':
            old_pointer = self.pointer
            self.pointer -= 1
            self.pointer %= self.total_lines
            if self.pointer in self.selection:
                self.selection.remove(old_pointer)
            self.selection.add(self.pointer)
        elif cn == b'KEY_SF' or cn == b'\x1b[1;2B': # Shift + Down
            old_pointer = self.pointer
            self.pointer += 1
            self.pointer %= self.total_lines
            if self.pointer in self.selection:
                self.selection.remove(old_pointer)
            self.selection.add(self.pointer)
        elif cn == b'KEY_LEFT':
            if self.lrpos > 0:
                self.lrpos -= 1
        elif cn == b'KEY_SLEFT' or cn == b'\x1b[1;2D':
            self.lrpos = max(0, self.lrpos - int(tabcols/2))
        elif cn == b'KEY_RIGHT':
            self.lrpos += 1
        elif cn == b'KEY_SRIGHT' or cn == b'\x1b[1;2C':
            self.lrpos = self.lrpos + int(tabcols/2)
        elif cn == b'KEY_PPAGE':
            self.top = max(self.top-nlines, 0)
            self.pointer = self.top
            self.selection = {self.pointer}
        elif cn == b'KEY_NPAGE':
            top = self.top + nlines
            self.pointer = min(top, self.total_lines-1)
            self.top = min(self.total_lines-nlines-1, top)
            self.selection = {self.pointer}
        elif cn == b'\x1b[5;2~': # Shift + PageUp
            self.top = max(self.top-nlines, 0)
            old_pointer = self.pointer
            self.pointer = max(self.pointer-nlines, self.top)
            new_sel = iter(range(old_pointer-1, self.pointer, -1))
            try:
                line = next(new_sel)
                if line in self.selection:
                    self.selection.remove(old_pointer)
                while line in self.selection:
                    self.selection.remove(line)
                    line = next(new_sel)
                while True:
                    self.selection.add(line)
                    line = next(new_sel)
            except StopIteration:
                pass
            self.selection.add(self.pointer)
        elif cn == b'\x1b[6;2~': # Shift + PageDown
            top = self.top + nlines
            old_pointer = self.pointer
            self.pointer = min(old_pointer+nlines, self.total_lines-1)
            self.top = min(self.total_lines-nlines-1, top)
            new_sel = iter(range(old_pointer+1, self.pointer))
            try:
                line = next(new_sel)
                if line in self.selection:
                    self.selection.remove(old_pointer)
                while line in self.selection:
                    self.selection.remove(line)
                    line = next(new_sel)
                while True:
                    self.selection.add(line)
                    line = next(new_sel)
            except StopIteration:
                pass
            self.selection.add(self.pointer)
        elif cn == b'KEY_HOME':
            self.pointer = self.top = 0
            self.selection = {self.pointer}
        elif cn == b'KEY_END':
            self.top = self.total_lines-nlines-1
            self.pointer = self.total_lines-1
            self.selection = {self.pointer}
        elif cn == b'u' and stack().canundo():
            stack().undo()
        elif cn == b'r' and stack().canredo():
            stack().redo()
        elif cn == b'\n':
            index = list(self.selection)
            gse = self.df["gse"].iloc[index[0]]
            file = os.path.join(self.softPath, gse, gse+'_family.soft')
            if os.path.exists(file):
                logging.info(f'Opening {file}')
                ids = self._id_for_index(list(self.selection))
                pattern = '|'.join(f'SAMPLE = {id}' for id in ids)
                less = f'less -p "{pattern}" "{file}"'
                os.system('tmux split-window -h {less}')
                os.system('tmux select-layout main-vertical')
            else:
                logging.error(f'Could not find {file}')
                self.error = f'Could not find soft file for {gse}.'
        else:
            if self.tags[self.current_tag]['type'] == 'int' \
                    and cn in self._byte_numbers:
                # set current tag to value
                self.set_tag(self.current_tag, int(cn), self._view_state)
                return
            for tag, info in self.tags.items():
                if cn == b'\x1b' + info['key'].encode():
                    if info['type'] == 'int':
                        logging.info(f'Starting to tag {tag}.')
                        self.current_tag = tag
                    elif info['type'] == 'str':
                        logging.info(f'Starting make a {tag}.')
                        self.make_str(tag)

    def make_str(self, tag):
        hight = min(23, curses.LINES-4)
        width = min(80, curses.COLS-4)
        editwin = curses.newwin(hight-3, width-3, 3, 3)
        rectangle(self.stdscr, 2, 2, hight, width)
        ids = self._id_for_index(list(self.selection))
        if len(ids)>1:
            id = f'[{ids[0]} ...]'
        else:
            id = ids[0]
        text = f"Enter {tag} for {id}: (hit Ctrl-G to send)"
        self.stdscr.addstr(2, 4, text[:(width-4)])
        self.stdscr.refresh()
        current_texts = self.get_current_values(tag)
        if len(current_texts) > 1:
            current_text = 'different text for selection'
        elif current_texts:
            current_text = current_texts.pop()
        else:
            current_text = None
        if current_text:
            editwin.addstr(str(current_text))
        box = Textbox(editwin)
        box.edit()
        message = box.gather().strip()
        if message != current_text:
            self.set_tag(tag, message, self._view_state)

    def _id_for_index(self, index):
        return self.df["id"].iloc[index]

    view_attributes = {
        'selection',
        'pointer',
        'col_pointer',
        'tag_pointer',
        'top',
        'filter',
        'show_columns',
        'sort_columns',
        'sort_reverse_columns',
        'ordered_columns',
        'color_by'
    }

    @property
    def _view_state(self):
        return {attr: getattr(self, attr) for attr in self.view_attributes}

    @_view_state.setter
    def _view_state(self, new_state):
        for critical in ['filter', 'sort_columns', 'sort_reverse_columns',
                         'color_by']:
            if new_state.get(critical) is not getattr(self, critical):
                self._update_now = True
        for key, value in new_state.items():
            setattr(self, key, value)

    @property
    def cache(self):
        return {'_view_state': self._view_state}

    @cache.setter
    def cache(self, cache):
        self._view_state = cache.get('_view_state')

    @property
    def data(self):
        return {
            'tag definitions':self.tags,
            'tags':self.tag_data
        }

    @data.setter
    def data(self, data):
        self.tag_data = data.get('tags', dict())
        self.tags.update(data.get('tag definitions', dict()))
        for tag in self.tags:
            self.tag_data.setdefault(tag, dict())

    def get_current_values(self, tag):
        selection = list(self.selection)
        ids = self._id_for_index(selection)
        td = self.tag_data[tag]
        return {td[id] for id in ids if id in td}

    @undoable
    def set_tag(self, tag, val, view_state):
        self._view_state = view_state
        lselected = list(self.selection)
        ids = self._id_for_index(lselected)
        td = self.tag_data[tag]
        current =  {id:td.get(id) for id in ids}
        if self.tags[tag]['type'] == 'str':
            log_val = val.splitlines()[0]
            if len(log_val) > 20:
                log_val = log_val[:17]+'...'
        else:
            log_val = val
        if len(ids) == 1:
            id = next(iter(ids))
            short_desc = long_desc = f'setting tag "{tag}" to '\
                                     f'"{log_val}" for {id}'
            short_desc = f'{tag}={log_val} for {id}'
        else:
            lids = list(ids)
            long_desc = f'setting tag "{tag}" to "{log_val}" for {lids}'
            short_desc = f'{tag}={log_val} for [{lids[0]}, ...]'
        logging.info(long_desc)
        for id, index in zip(ids, lselected):
            td[id] = val
            self.df.loc[self.df.index[index], tag] = val
        self.save_tag_data()
        self.stale_lines.update(self.selection)
        yield short_desc
        logging.info('undoing '+long_desc)
        for ind, (id, v) in enumerate(current.items()):
            if v is None or np.isnan(v):
                del td[id]
                self.df.loc[self.df.index[lselected[ind]], tag] = '-'
            else:
                td[id] = v
                self.df.loc[self.df.index[lselected[ind]], tag] = v
        self.save_tag_data()
        self.stale_lines.update(view_state['selection'])
        self._view_state = view_state

    def save_tag_data(self):
        if self.last_saver_pid:
            try:
                _, exit_code = os.waitpid(self.last_saver_pid, 0)
            except ChildProcessError:
                pass
            if exit_code != 0:
                self.error = f'Could not save!! log: {self.log}'
        self.last_saver_pid = os.fork()
        if self.last_saver_pid == 0:
            save = self.data
            try:
                with open(self.output+'.tmp', 'w') as f:
                    f.write(yaml.dump(save))
                os.rename(self.output+'.tmp', self.output)
            except BaseException as e:
                logging.error('Error writing tag data.')
                logging.error(e)
                os._exit(1)
            os._exit(0)

    def _print_help(self):
        help = self.helptext
        hight = min(len(help)+1, curses.LINES-4)
        width = min(80, curses.COLS-4)
        self.win = self.stdscr.subwin(hight, width, 2, 2)
        self.win.clear()
        self.win.border()
        for i in range(1, hight-1):
            self.win.addstr(i, 5, help[i].strip()[:width-6])
        if hight-1 < len(help):
            self.win.addstr(i, 1, ' '*(width-2))
            self.win.addstr(i, 5, '...'[:width-6])

    _window_width = 120

    def _view_dialoge(self):
        self.table_y0 = 5
        self.table_x0 = 6
        hight = min(len(self.ordered_columns)+self.table_y0+2, curses.LINES-4)
        width = min(self._window_width, curses.COLS-4)
        self.win = self.stdscr.subwin(hight, width, 2, 2)
        self.win.clear()
        self.win.border()
        buttons = {
            'f':'exit',
            'Enter':'edit regex',
            'r':'reset',
            'd':'toggle deactivate',
            's':'toggle sort by',
            'c':'toggle color by',
            'shift+up/down':'change order'
        }
        self.win.move(1, self.table_x0-2)
        for key, desc in buttons.items():
            hintlen = len(key)+len(desc)+4
            _, x = self.win.getyx()
            if x+hintlen > width-2:
                break
            self.win.addstr('  ')
            self.win.addstr(key+':', curses.color_pair(100))
            self.win.addstr(' '+desc)
        legend = {
            'legend:':103,
            'deactivated':102,
            'sorted by':101,
            'reverse sorted by':105,
            'color by':104
        }
        self.win.move(2, self.table_x0-2)
        for lable, col in legend.items():
            hintlen = len(lable)+2
            _, x = self.win.getyx()
            if x+hintlen > width-2:
                break
            self.win.addstr('  ')
            self.win.addstr(lable, curses.color_pair(col))
        self.indentation = max(len(c) for c in self.ordered_columns)
        ind = width-self.table_x0-1 if width-1>self.table_x0 else 0
        header = 'column' + ' '*(self.indentation-5) + 'regex'
        self.win.addstr(4, self.table_x0, header[:ind])
        self.woffset = max(0, self.col_pointer-hight+self.table_y0+3)
        for i, col in enumerate(self.ordered_columns):
            if i < self.woffset:
                continue
            elif self.woffset > 0 and i == self.woffset:
                self.win.addstr(self.table_y0, self.table_x0, '...'[:ind])
                continue
            ypos = i+self.table_y0-self.woffset
            if ypos+2 == hight:
                self.win.addstr(ypos, self.table_x0, '...'[:ind])
                break
            attr = curses.A_REVERSE if i==self.col_pointer else curses.A_NORMAL
            if col not in self.show_columns:
                attr |= curses.color_pair(legend['deactivated'])
            elif col == self.color_by:
                attr |= curses.color_pair(legend['color by'])
            elif col in self.sort_columns:
                attr |= curses.color_pair(legend['sorted by'])
            elif col in self.sort_reverse_columns:
                attr |= curses.color_pair(legend['reverse sorted by'])
            filter = self.filter.get(col, '*')
            text = col + ' '*(self.indentation-len(col)+1) + filter
            self.win.addstr(ypos, self.table_x0, text[:ind], attr)
            if col in self.tag_data:
                self.win.addstr(ypos, 2, 'tag')

    def _dialog(self, cn):
        if cn == b'f':
            self.in_dialog = False
            self.stdscr.addstr(0, 0,
                               'Loading ...'.ljust(curses.COLS)[:curses.COLS-1])
            self.stdscr.refresh()
            if self._dialog_changed:
                self._update_now = True
        elif cn == b'\n':
            col = self.ordered_columns[self.col_pointer]
            ypos = self.col_pointer + self.table_y0 - self.woffset + 2
            xpos = self.indentation + self.table_x0 + 3
            width = self._window_width - xpos
            editwin = self.stdscr.subwin(1, width, ypos, xpos)
            editwin.addstr(0, 0, self.filter.get(col, ''))
            self.stdscr.refresh()
            box = Textbox(editwin)
            box.edit()
            filter = box.gather().strip()
            if filter not in ['*', '']:
                logging.info(f'Setting filter for "{col}": "{filter}"')
                self.filter[col] = filter
            else:
                logging.info(f'Setting empty filter for "{col}".')
                self.filter.pop(col, None)
            self._dialog_changed = True
        elif cn == b'KEY_UP':
            self.col_pointer -= 1
            self.col_pointer %= len(self.ordered_columns)
        elif cn == b'KEY_DOWN':
            self.col_pointer += 1
            self.col_pointer %= len(self.ordered_columns)
        elif cn == b'r':
            col = self.ordered_columns[self.col_pointer]
            logging.info(f'Resetting filter for "{col}".')
            if col in self.filter:
                self.filter.pop(col, None)
                self._dialog_changed = True
        elif cn == b'd':
            col = self.ordered_columns[self.col_pointer]
            if col in self._required_columns:
                logging.debug(f'Cannot deactivate "{col}".')
            elif col in self.show_columns:
                logging.info(f'Deactivate "{col}".')
                self.show_columns.remove(col)
            else:
                logging.info(f'Activate "{col}".')
                self.show_columns.add(col)
            self._dialog_changed = True
        elif cn == b's':
            col = self.ordered_columns[self.col_pointer]
            if col in self.sort_columns:
                logging.info(f'Reverse sort by "{col}".')
                self.sort_columns.remove(col)
                self.sort_reverse_columns.add(col)
            elif col in self.sort_reverse_columns:
                logging.info(f'Do not sort by "{col}".')
                self.sort_reverse_columns.remove(col)
            else:
                logging.info(f'Sort by "{col}".')
                self.sort_columns.add(col)
            self._dialog_changed = True
        elif cn == b'KEY_SR' or cn == b'\x1b[1;2A':
            col_val = self.ordered_columns[self.col_pointer]
            logging.info(f'Moving column "{col_val}" up.')
            col_pos = self.col_pointer
            self.col_pointer -= 1
            if self.col_pointer < 0:
                self.col_pointer = len(self.ordered_columns)-1
                self.ordered_columns.remove(col_val)
                self.ordered_columns.append(col_val)
            else:
                self.ordered_columns[col_pos] = \
                        self.ordered_columns[self.col_pointer]
                self.ordered_columns[self.col_pointer] = col_val
                self._dialog_changed = True
        elif cn == b'KEY_SR' or cn == b'\x1b[1;2B':
            col_val = self.ordered_columns[self.col_pointer]
            logging.info(f'Moving column "{col_val}" down.')
            col_pos = self.col_pointer
            self.col_pointer += 1
            if self.col_pointer >= len(self.ordered_columns):
                self.col_pointer = 0
                self.ordered_columns.remove(col_val)
                self.ordered_columns = [col_val] + self.ordered_columns
                del self.ordered_columns[-1]
            else:
                self.ordered_columns[col_pos] = \
                        self.ordered_columns[self.col_pointer]
                self.ordered_columns[self.col_pointer] = col_val
                self._dialog_changed = True
        elif cn == b'c':
            col = self.ordered_columns[self.col_pointer]
            logging.info(f'Coloring by "{col}".')
            if self.color_by == col:
                self.color_by = False
            else:
                self.color_by = col
            self._dialog_changed = True

    def _view_tag_dialoge(self):
        self.table_y0 = 4
        self.table_x0 = 2
        hight = min(len(self.ordered_columns)+self.table_y0+2, curses.LINES-4)
        width = min(self._window_width, curses.COLS-4)
        self.win = self.stdscr.subwin(hight, width, 2, 2)
        self.win.clear()
        self.win.border()
        buttons = {
            't':'exit',
            'Enter':'edit tag',
            'n':'new tag',
            'd':'delete tag',
            'u':'undo',
            'r':'redo'
        }
        self.win.move(1, self.table_x0-1)
        for key, desc in buttons.items():
            hintlen = len(key)+len(desc)+4
            _, x = self.win.getyx()
            if x+hintlen > width-2:
                break
            self.win.addstr(' ')
            self.win.addstr(key+':', curses.color_pair(100))
            self.win.addstr(' '+desc)
        if self.tag_error:
            self.win.addstr(2, self.table_x0, self.tag_error,
                            curses.color_pair(102))
            self.tag_error = None
        self.indentation = {'name': max(len(c) for c in self.tags)}
        header = 'name'.ljust(self.indentation['name'])
        for tc in tag_characteristics:
            field_len = max(len(str(v[tc])) for v in self.tags.values())
            field_len = max(len(tc), field_len)
            header += ' '+tc.ljust(field_len)
            self.indentation[tc] = field_len
        ind = width-self.table_x0-1 if width-1>self.table_x0 else 0
        self.win.addstr(3, self.table_x0, header[:ind])
        self.woffset = max(0, self.tag_pointer-hight+self.table_y0+3)
        for i, tag in enumerate(sorted(self.tags)):
            if i < self.woffset:
                continue
            elif self.woffset > 0 and i == self.woffset:
                self.win.addstr(self.table_y0, self.table_x0, '...'[:ind])
                continue
            ypos = i+self.table_y0-self.woffset
            if ypos+2 == hight:
                self.win.addstr(ypos, self.table_x0, '...'[:ind])
                break
            attr = curses.A_REVERSE if i==self.tag_pointer else curses.A_NORMAL
            text = tag.ljust(self.indentation['name'])
            for tc in tag_characteristics:
                content = str(self.tags[tag].get(tc, ''))
                indent = self.indentation[tc]
                text += ' '+content.ljust(indent)
            self.win.addstr(ypos, self.table_x0, text[:ind], attr)
        if self.add_tag:
            self._tag_edit()

    def _tag_dialog(self, cn):
        if self.serious and cn != b'd':
            self.serious = False
        if cn == b't':
            self.in_tag_dialog = False
            self.stdscr.addstr(0, 0,
                               'Loading ...'.ljust(curses.COLS)[:curses.COLS-1])
            self.stdscr.refresh()
        elif cn == b'\n':
            for i, tag in enumerate(sorted(self.tags)):
                if i==self.tag_pointer:
                    break
            self._tag_edit(tag)
        elif cn == b'n':
            self.tag_pointer = len(self.tags)
            self.add_tag = True
        elif cn == b'd':
            for i, tag in enumerate(sorted(self.tags)):
                if i==self.tag_pointer:
                    break
            if self.tag_data.get(tag) and not self.serious:
                self.tag_error = f'The tag {tag} contains data. ' \
                                 'Press d again if you realy want to delete it.'
                self.serious = True
            else:
                self.remove_tag(tag)
                self.serious = False
        elif cn == b'KEY_UP':
            self.tag_pointer -= 1
            self.tag_pointer %= len(self.tags)
        elif cn == b'KEY_DOWN':
            self.tag_pointer += 1
            self.tag_pointer %= len(self.tags)
        elif cn == b'u' and stack().canundo():
            stack().undo()
        elif cn == b'r' and stack().canredo():
            stack().redo()

    def _tag_edit(self, tag_name=None):
        info = self.tags.get(tag_name, dict())
        if tag_name is None:
            tag_name = ''
        ypos = self.tag_pointer + self.table_y0 - self.woffset + 2
        xpos = self.table_x0+2
        total_width = min(self._window_width, curses.COLS-4)
        ind = total_width - xpos if total_width>xpos else 0
        def print_status(status, color=101):
            self.stdscr.addstr(4, self.table_x0+2, status.ljust(ind)[:ind],
                               curses.color_pair(color))
            self.stdscr.refresh()
        def get_value(default):
            width = total_width - xpos
            self.stdscr.addstr(ypos, xpos, ' '*width)
            editwin = self.stdscr.subwin(1, width, ypos, xpos)
            editwin.addstr(0, 0, default)
            self.stdscr.refresh()
            box = Textbox(editwin)
            box.edit()
            return box.gather().strip()
        if not tag_name:
            print_status('Enter a name!')
            tag_name = get_value(tag_name)
        xpos += self.indentation['name']+1
        new_info = dict()
        print_status('Press a letter key!')
        used_keyes = {t['key'] for t in self.tags.values()}
        current_key = info.get('key', '')
        used_keyes -= {current_key}
        while True:
            self.stdscr.addstr(ypos, xpos, current_key)
            self.stdscr.refresh()
            key = self.stdscr.getkey(ypos, xpos)
            if key in used_keyes:
                print_status('Press an unused letter key!', 102)
            elif key in 'abcdefghijklmnopqrstuvwxyz':
                break
            else:
                print_status('Press a letter key!', 102)
        new_info['key'] = key
        self.stdscr.addstr(ypos, xpos, key)
        xpos += self.indentation['key']+1
        print_status('Enter a type!')
        ct = info.get('type', '')
        while True:
            ct = get_value(ct)
            if ct in ['int', 'str']:
                break
            print_status('Please enter "int" for integer or "str" for string!',
                         102)
        new_info['type'] = ct
        xpos += self.indentation['type']+1
        print_status('Enter a column width!')
        cw = info.get('col_width', '')
        while True:
            cw = get_value(str(cw))
            if cw.isnumeric() and int(cw)>0:
                break
            print_status('Please enter a positive integer!', 102)
        new_info['col_width'] = int(cw)
        xpos += self.indentation['col_width']+1
        print_status('Enter a description!')
        new_info['desc'] = get_value(info.get('desc', ''))
        self.set_tag_definition(tag_name, new_info)
        for i, tag in enumerate(sorted(self.tags)):
            self.tag_pointer = i
            if tag == tag_name:
                break

    @undoable
    def set_tag_definition(self, tag_name, new_info):
        old_info = self.tags.get(tag_name)
        self.tags[tag_name] = new_info
        if old_info is None:
            self.tag_data.setdefault(tag_name, dict())
            self.ordered_columns = [tag_name] + self.ordered_columns
            self.show_columns.add(tag_name)
            self.df.insert(0, tag_name, '-')
            desc = f'create tag {tag_name}.'
        else:
            desc = f'edit tag {tag_name}.'
        self.header = self._str_from_line()
        self.stale_lines = set(range(self.total_lines))
        logging.info(desc)
        self.save_tag_definitions()
        yield desc
        logging.info('undoing ' + desc)
        if old_info is None:
            self.ordered_columns.remove(tag_name)
            self.show_columns.remove(tag_name)
            del self.tags[tag_name]
            del self.tag_data[tag_name]
            del self.df[tag_name]
        else:
            self.tags[tag_name] = old_info
        self.header = self._str_from_line()
        self.stale_lines = set(range(self.total_lines))
        self.save_tag_definitions()

    @undoable
    def remove_tag(self, tag_name):
        old_def = self.tags[tag_name]
        old_data = self.tag_data[tag_name]
        del self.tags[tag_name]
        del self.tag_data[tag_name]
        self.show_columns -= {tag_name}
        self.ordered_columns.remove(tag_name)
        df_dat = None
        if tag_name in self.df:
            df_dat = self.df[tag_name]
            del self.df[tag_name]
            self.stale_lines = set(range(self.total_lines))
            self.header = self._str_from_line()
        desc = f'remove tag {tag_name}'
        logging.info(desc)
        self.save_tag_definitions()
        yield desc
        logging.info('undoing '+desc)
        self.tags[tag_name] = old_def
        self.tag_data[tag_name] = old_data
        self.ordered_columns = [tag_name] + self.ordered_columns
        self.show_columns.add(tag_name)
        self.df.insert(0, tag_name, '-')
        if df_dat is not None:
            self.df[tag_name] = df_dat
        self.header = self._str_from_line()
        self.stale_lines = set(range(self.total_lines))
        self.save_tag_definitions()

    _helptext = """
        h             Show/hide help window.
        q             Save and quit geotag.
        f             Filter dialoge.
        t             Tag dialoge.
        Up            Move upward.
        Down          Move downward.
        Shift+Up      Select upward.
        Shift+Down    Select downward.
        Pageup        Move upward one page.
        Pagedown      Move down one page.
        Shift+Pageup  Move upward one page.
        ShiftPagedown Move down one page.
        Left          Move to the left hand side.
        Right         Move to the right hand side.
        Shift+Left    Move to the left by half a page.
        Shift+Right   Move to the right by half a page.
        Home          Move to the start of the table.
        End           Move to the end of the table.
        Ctrl+a        Select all.
        """.splitlines()

    @property
    def helptext(self):
        h = self._helptext[:]
        indent = 14
        for tag, info in self.tags.items():
            keys = 'Alt+' + info['key']
            space = ' '*max(1, indent-len(keys))
            if info['type'] == 'str':
                h.append(keys+space+'Make a '+tag+'.')
            else:
                h.append(keys+space+'Start tagging '+tag+'.')
        return h

if __name__ == "__main__":
    main()
