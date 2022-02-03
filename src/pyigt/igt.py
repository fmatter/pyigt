import re
import json
import shutil
import pathlib
import argparse
import tempfile
import collections
import unicodedata

from tabulate import tabulate
import lingpy
import segments
import attr
from clldutils.misc import nfilter
from clldutils.lgr import ABBRS, PERSONS, pattern
from csvw.dsv import UnicodeWriter, reader
from csvw.metadata import Link
from pycldf import Dataset
import pycldf

__all__ = ['IGT', 'Corpus', 'CorpusSpec', 'is_standard_abbr', 'expand_standard_abbr']

STANDARD_ABBR_PATTERN = pattern()
NON_OVERT_ELEMENT = '∅'


def is_standard_abbr(label):
    match = STANDARD_ABBR_PATTERN.fullmatch(label)
    if match:
        return not bool(match.group('pre'))
    return False


def expand_standard_abbr(label):
    match = STANDARD_ABBR_PATTERN.fullmatch(label)
    if match and not match.group('pre'):
        res = ''
        if match.group('person'):
            res += PERSONS[match.group('person')] + ' '
        return res + ABBRS.get(match.group('abbr'))
    return label


def _morpheme_and_infixes(m):
    morpheme, infixes, infix, in_infix = [], [], [], False

    if m.startswith('<'):
        morpheme.append('')

    for c in m:
        if c == '<':
            in_infix = True
            continue
        elif c == '>' and in_infix:
            infixes.append(''.join(infix))
            infix = []
            in_infix = False
            continue

        if in_infix:
            infix.append(c)
        else:
            morpheme.append(c)

    if in_infix:
        return [m]
    return [''.join(morpheme)] + infixes


def iter_morphemes(s, split_infixes=True):
    """
    Split word into morphemes following the Leipzig Glossing Rules:
    - `-` and `=` (for clitics) separate morphemes, see LGR rule 2.
    - `~` splits reduplicated morphemes, see LGR rule 10.
    - `<` and `>` enclose infixes, see LGR rule 9.
    """
    morpheme, separator = [], None

    for c in s:
        if c in {'-', '=', '~'}:
            if split_infixes:
                yield from _morpheme_and_infixes(''.join(morpheme))
            else:
                yield (separator, ''.join(morpheme))
            morpheme = []
            separator = c
        else:
            morpheme.append(c)

    if split_infixes:
        yield from _morpheme_and_infixes(''.join(morpheme))
    else:
        yield (separator, ''.join(morpheme))


@attr.s
class CorpusSpec(object):
    punctuation = attr.ib(
        validator=attr.validators.instance_of(list),
        default=list(',;.”“"()'))
    concept_replace = attr.ib(
        validator=attr.validators.instance_of(dict),
        default={'.': ' ', '†(': '', '†': ''})
    paradigm_marker = attr.ib(
        validator=attr.validators.instance_of(str),
        default=':')
    # A "simple" `CorpusSpec`, only recognizing one single-character morpheme separator, can be
    # constructed by setting `morpheme_separator`.
    morpheme_separator = attr.ib(
        validator=attr.validators.optional(attr.validators.instance_of(str)),
        default=None)
    label_pattern = attr.ib(default=re.compile('^([A-Z][A-Z0-9]*|([1-3](DL|PL|SG|DU)))$'))

    def split_morphemes(self, s, split_infixes=True):
        if self.morpheme_separator:
            return s.split(self.morpheme_separator)
        return list(iter_morphemes(s, split_infixes=split_infixes))

    def strip_punctuation(self, s):
        for p in self.punctuation:
            s = s.replace(p, '')
        return s

    def is_grammatical_gloss_label(self, s):
        return bool((s in ABBRS) or self.label_pattern.match(s))

    def clean_concept(self, c):
        for k, v in self.concept_replace.items():
            c = c.replace(k, v)
        return c.strip()

    def _glosses(self, concept, ctype):
        return [
            g for g in nfilter(self.clean_concept(cn) for cn in concept.split(self.paradigm_marker))
            if (ctype == 'grammar' and self.is_grammatical_gloss_label(g))
            or (ctype == 'lexicon' and not self.is_grammatical_gloss_label(g))]  # noqa: W503

    def lexical_gloss(self, concept):
        return ' // '.join(self._glosses(concept, 'lexicon'))

    def grammatical_glosses(self, concept):
        return self._glosses(concept, 'grammar')


def parse_phrase(p):
    if isinstance(p, str):
        rule2a = re.compile(r'([^\s]+) -')
        return [
            w.replace('|||', ' ') for w in rule2a.sub(lambda m: m.groups()[0] + '|||-', p).split()]
    return p


@attr.s
class IGT(object):
    """
    Interlinear Glossed Text

    The main trait of IGT is the alignment of words and glosses. Thus, we are mostly interested
    in the two aligned "lines": the analyzed text and the glosses, rather than trying to support
    any number of tiers, and alignment based on timestamps or similar.
    """
    phrase = attr.ib(
        validator=attr.validators.instance_of(list),
        converter=parse_phrase,
    )
    gloss = attr.ib(
        validator=attr.validators.instance_of(list),
        converter=lambda g: g.split() if isinstance(g, str) else g,
    )
    id = attr.ib(default=None)
    properties = attr.ib(validator=attr.validators.instance_of(dict), default=attr.Factory(dict))
    language = attr.ib(default=None)
    translation = attr.ib(default=None)
    spec = attr.ib(default=CorpusSpec())
    abbrs = attr.ib(validator=attr.validators.instance_of(dict), default=attr.Factory(dict))

    def __attrs_post_init__(self):
        self.phrase_segmented = [nfilter(self.spec.split_morphemes(m)) for m in self.phrase]
        self.gloss_segmented = [nfilter(self.spec.split_morphemes(m)) for m in self.gloss]
        self.phrase_segmented2 = [
            nfilter(self.spec.split_morphemes(m, split_infixes=False)) for m in self.phrase]
        self.gloss_segmented2 = [
            nfilter(self.spec.split_morphemes(m, split_infixes=False)) for m in self.gloss]
        if self.translation:
            p = re.compile(r'\((?P<abbrs>((\s*,\s*)?[A-Z][A-Z0-9]*\s*=\s*[^,)]+)+)\)')
            abbrs = p.search(self.translation)
            if abbrs:
                for abbr in abbrs.group('abbrs').split(','):
                    abbr, _, label = abbr.partition('=')
                    self.abbrs[abbr.strip()] = label.strip()
                self.translation = p.sub('', self.translation).strip()
                if self.translation[0] == "'" or unicodedata.category(self.translation[0]) == 'Pi':
                    # Punctuation, Initial quote
                    self.translation = self.translation[1:].strip()
                if self.translation[-1] == "'" or \
                        unicodedata.category(self.translation[-1]) == 'Pf':
                    # Punctuation, Final quote
                    self.translation = self.translation[:-1].strip()

    @property
    def glossed_words(self):
        return list(zip(self.phrase, self.gloss))

    @property
    def glossed_morphemes(self):
        res = []
        for word, word_gloss in zip(self.phrase_segmented2, self.gloss_segmented2):
            morpheme_glosses = []
            for (_, m), (_, g) in zip(word, word_gloss):
                morpheme_glosses.append((m, g))
            res.append(morpheme_glosses)
        return res

    @property
    def gloss_abbrs(self):
        res = collections.OrderedDict()
        for word in self.glossed_morphemes:
            for m, g in word:
                for element in re.split(r'[.;:\\<>()\[\]]', g):
                    if element:
                        if self.spec.is_grammatical_gloss_label(element):
                            if element in self.abbrs:
                                res[element] = self.abbrs[element]
                            else:
                                res[element] = expand_standard_abbr(element)
        return res

    def __str__(self):
        return '{0}\n{1}{2}'.format(
            self.primary_text,
            tabulate([self.gloss], self.phrase, tablefmt='plain'),
            '\n‘{}’'.format(self.translation) if self.translation else '',
        )

    def pprint(self):
        abbrs = [(k, v) for k, v in self.gloss_abbrs.items() if v]
        if abbrs:
            mlen = max(len(a[0]) for a in abbrs)
            abbrs = ''.join('\n  {} = {}'.format(k.ljust(mlen), v) for k, v in abbrs)
        print('{}{}'.format(self, abbrs or ''))

    def __getitem__(self, item):
        """
        Provide access to individual (word, gloss) or (morpheme, gloss) pairs.

        :param item: An `int` index to reference a (word, gloss) pair or a (`int`, `int`) tuple,\
        referencing a (morpheme, gloss) pair.
        :return: (word, gloss) or (morpheme, gloss) pair
        """
        if not isinstance(item, tuple):
            return self.glossed_words[item]
        return self.glossed_morphemes[item[0]][item[1]]

    def is_valid(self, strict=False):
        try:
            self.check(strict=strict)
            return True
        except ValueError:
            return False

    def check(self, strict=False, verbose=False):
        """
        :param strict: If `True`, also check Rule 2: Morpheme-by-morpheme correspondence.
        """
        res = len(self.phrase) == len(self.gloss)
        if not res:
            if verbose:
                print('\t'.join(self.phrase))
                print('\t'.join(self.gloss))
            raise ValueError(
                'Rule 1 violated: Number of words does not match number of word glosses!')
        if strict:
            for i, (m, g) in enumerate(zip(self.phrase_segmented2, self.gloss_segmented2)):
                if len(m) != len(g):
                    if verbose:
                        print(self.phrase[i])
                        print(self.gloss[i])
                    raise ValueError(
                        'Rule 2 violated: Number of morphemes does not match number of morpheme '
                        'glosses!')
                if [mm[0] for mm in m] != [gg[0] for gg in g]:
                    if verbose:
                        print(self.phrase[i])
                        print(self.gloss[i])
                    raise ValueError(
                        'Rule 2 or 10 violated: Mismatch of element separators in word and gloss!')

    @property
    def phrase_text(self):
        return ' '.join(self.phrase)

    @property
    def primary_text(self):
        return ' '.join(''.join(
            m[0] for m in w if m[0] != NON_OVERT_ELEMENT) for w in self.glossed_morphemes)

    @property
    def gloss_text(self):
        return ' '.join(self.gloss)


class Concordances(argparse.Namespace):
    def __getitem__(self, item):
        return vars(self)[item]


class Corpus(object):
    """
    A Corpus is an immutable, ordered list of `IGT` instances.
    """
    def __init__(self, igts, spec=None, fname=None):
        self.fname = fname
        self.spec = spec or CorpusSpec()
        self._igts = collections.OrderedDict([(igt.id, igt) for igt in igts])
        self._concordances = Concordances(
            grammar=collections.defaultdict(list),
            lexicon=collections.defaultdict(list),
            form=collections.defaultdict(list),
        )
        # Since changing the IGTs in the corpus is not allowed, we can compute concordances right
        # away.
        for idx, igt in self._igts.items():
            if not igt.is_valid():
                continue
            for i, (w, m) in enumerate(zip(igt.phrase_segmented, igt.gloss_segmented)):
                if len(w) != len(m):
                    continue
                for j, (morpheme, concept) in enumerate(zip(w, m)):
                    morpheme = self.spec.strip_punctuation(morpheme)
                    if not morpheme:
                        continue
                    ref = (idx, i, j)
                    for g in self.spec.grammatical_glosses(concept):
                        self._concordances.grammar[morpheme, g, concept, igt.language].append(ref)
                    self._concordances.lexicon[
                        morpheme, self.spec.lexical_gloss(concept), concept, igt.language
                    ].append(ref)
                    self._concordances.form[morpheme, concept, concept, igt.language].append(ref)
        self.monolingual = len(set(igt.language for igt in self._igts.values())) == 1

    @staticmethod
    def get_column_names(cldf):
        # We lookup local column names by ontology term:
        lookup = [
            ('id', 'id'),
            ('phrase', 'analyzedWord'),
            ('gloss', 'gloss'),
            ('translation', 'translatedText'),
            ('language', 'languageReference'),
        ]
        names = collections.namedtuple('colnames', [k for k, v in lookup])
        return names(**{
            k: cldf['ExampleTable', v].name if ('ExampleTable', v) in cldf else None
            for k, v in lookup})

    @classmethod
    def from_cldf(cls, cldf, spec=None):
        """
        A corpus of IGT examples provided with a CLDF dataset.

        :param cldf: a `pycldf.Dataset` instance.
        :param spec: a `CorpusSpec` instance, specifying how to interpret markup in the corpus.
        """
        spec = spec or CorpusSpec()
        cols = cls.get_column_names(cldf)
        igts = [
            IGT(
                id=igt[cols.id],
                gloss=igt[cols.gloss],
                phrase=igt[cols.phrase],
                language=igt.get(cols.language),
                translation=igt.get(cols.translation),
                properties=igt,
                spec=spec,
            )
            for igt in cldf['ExampleTable']]
        return cls(
            igts,
            spec=spec,
            fname=cldf.tablegroup._fname.parent / str(cldf['ExampleTable'].url))

    @classmethod
    def from_stream(cls, stream, spec=None):
        from csvw.metadata import TableGroup
        cldf = Dataset(TableGroup(fname=pathlib.Path('tmp.json')))
        cldf.add_component('ExampleTable')

        spec = spec or CorpusSpec()
        cols = cls.get_column_names(cldf)
        igts = [
            IGT(
                id=igt[cols.id],
                gloss=igt[cols.gloss].split('\\t'),
                phrase=igt[cols.phrase].split('\\t'),
                language=igt.get(cols.language),
                properties=igt,
                spec=spec,
            )
            for igt in reader(stream.read().splitlines(), dicts=True)]
        return cls(igts, spec=spec)

    @classmethod
    def from_path(cls, path, spec=None):
        """
        Instantiate a corpus from a file path.

        :param path: Either a path to a CLDF dataset's metadata file or to a CLDF Examples \
        component as CSV file. Note that in the latter case, the file must use the default \
        column names, as defined in the CLDF ontology.
        :return: `Corpus` instance.
        """
        if isinstance(path, str):
            path = pathlib.Path(path)
        if path.suffix == '.json':
            return cls.from_cldf(Dataset.from_metadata(path), spec=spec)
        # We are given only an ExampleTable. Let's create the appropriate dataset:
        header = None
        for d in reader(path, dicts=True):
            header = list(d.keys())
            break
        ds = Dataset.from_metadata(
            pathlib.Path(pycldf.__file__).parent / 'modules' / 'Generic-metadata.json')
        ds.tablegroup._fname = path.parent / 'cldf-metadata.json'
        t = ds.add_component('ExampleTable')
        t.url = Link(path.name)
        default_cols = [col.name for col in t.tableSchema.columns]
        ds.remove_columns(t, *list(set(default_cols) - set(header)))
        ds.add_columns(t, *list(set(header) - set(default_cols)))
        return cls.from_cldf(ds, spec=spec)

    def __len__(self):
        return len(self._igts)

    def __iter__(self):
        return iter(self._igts.values())

    def __getitem__(self, item):
        if not isinstance(item, tuple):
            return self._igts[item]
        if len(item) == 2:
            return self._igts[item[0]][item[1]]
        return self._igts[item[0]][tuple(item[1:])]

    def get_stats(self):
        wordc, morpc = 0, 0
        for igt in self:
            for word in igt.phrase_segmented:
                wordc += 1
                morpc += len(word)
        return len(self._igts), wordc, morpc

    def write_concordance(self, ctype, filename=None):
        with UnicodeWriter(filename, delimiter='\t') as w:
            h = ['ID', 'FORM', 'GLOSS', 'GLOSS_IN_SOURCE', 'OCCURRENCE', 'REF']
            if not self.monolingual:
                h.insert(1, 'LANGUAGE_ID')
            w.writerow(h)

            for i, (k, v) in enumerate(sorted(
                    self._concordances[ctype].items(), key=lambda x: (-len(x[1]), x[0])), start=1):
                c = [
                    i,
                    k[0],
                    k[1],
                    k[2],
                    len(v),
                    ' '.join(['{0}:{1}:{2}'.format(x, y, z) for x, y, z in v])]
                if not self.monolingual:
                    c.insert(1, k[3])
                w.writerow(c)

        if not filename:
            print(w.read().decode('utf8'))

    def get_concepts(self, ctype='lexicon'):
        concepts = collections.defaultdict(list)

        for (form, c1, c2, l), occs in self._concordances[ctype].items():
            # get occurrence, and one example
            assert form
            concepts[c1].append((form, l, c2, len(occs)))

        return concepts

    def write_concepts(self, ctype, filename=None):
        def format_form(f):
            if self.monolingual:
                return f[0]
            return '{}: {}'.format(f[1], f[0])

        concepts = self.get_concepts(ctype)
        with UnicodeWriter(filename, delimiter='\t') as w:
            w.writerow(
                ['ID', 'ENGLISH', 'OCCURRENCE', 'CONCEPT_IN_SOURCE', 'FORMS', 'PHRASE', 'GLOSS'])
            for i, (concept, forms) in enumerate(
                    sorted(concepts.items(), key=lambda x: x[1][0][-1], reverse=True), start=1):
                # Get the IGT containing the first occurrence listed in the concordance as example:
                igt = self[
                    self._concordances[ctype][
                        forms[0][0],
                        concept,
                        forms[0][2],
                        forms[0][1],
                    ][0][0]
                ]
                w.writerow([
                    i,
                    concept,
                    sum([f[3] for f in forms]),
                    ' // '.join(sorted(set([f[2] for f in forms]))),
                    ' // '.join(sorted(set([format_form(f) for f in forms]))),
                    igt.phrase_text,
                    igt.gloss_text,
                ])
        if not filename:
            print(w.read().decode('utf8'))

    def check_glosses(self, level=2):
        count = 1
        for idx, igt in self._igts.items():
            if not igt.is_valid() and level >= 1:
                print('[{0} : first level {1}]'.format(idx, count))
                print(igt.phrase)
                print(igt.gloss_segmented)
                print('---')
                count += 1
            if level >= 2:
                for i, (w, m) in enumerate(zip(igt.phrase_segmented, igt.gloss_segmented), start=1):
                    if len(w) != len(m):
                        print('[{0}:{1} : second level {2}]'.format(idx, i, count))
                        print(w)
                        print(m)
                        print('---')
                        count += 1

    def get_wordlist(
            self,
            doculect='base',
            profile=False,
            ref='crossid',
            lexstat=True,
            threshold=0.4):
        """
        Return a classical wordlist from the data.
        """
        if profile:
            profile = segments.Tokenizer(profile)
            tokenize = lambda x: profile('^' + x + '$', column='IPA').split()  # noqa: E731
        else:
            tokenize = lingpy.ipa2tokens

        D = {
            0: [
                'doculect',
                'concept',
                'concept_in_source',
                'concept_type',
                'form',
                'tokens',
                'occurrences',
                'word_forms',
                'gloss_forms',
                'phrase_example',
                'gloss_example',
                'references',
            ]
        }
        idx = 1
        for ctype in ['lexicon', 'grammar']:
            concepts = self.get_concepts(ctype=ctype)
            concordance = self._concordances[ctype]
            for concept, entries in concepts.items():
                for form, lid, cis, freq in entries:
                    # retrieve the concordance
                    pidx, sA, sB = concordance[form, concept, cis, lid][0]
                    txt = self[pidx].phrase
                    gls = self[pidx].gloss
                    word, fgls = self[pidx, sA]
                    tokens = tokenize(form)
                    references = ' '.join(
                        ['{0}:{1}:{2}'.format(a, b, c)
                         for a, b, c in concordance[form, concept, cis, lid]])
                    # check tokens
                    try:
                        lingpy.tokens2class(tokens, 'sca')
                        check = True
                    except:  # noqa: E722, # pragma: no cover
                        check = False
                    if concept.strip() and check:
                        D[idx] = [
                            doculect if self.monolingual else lid,
                            concept,
                            cis,
                            ctype,
                            form,
                            tokens,
                            freq,
                            word,
                            fgls,
                            txt,
                            gls,
                            references]
                        idx += 1
                    else:
                        print('[!] Problem with "{0}" / [{1}] [{2}] / {3} {4} {5}'.format(
                            concept,
                            form,
                            tokens,
                            pidx,
                            sA,
                            sB,
                        ))
        wl = lingpy.Wordlist(D)

        if lexstat:
            wl = lingpy.LexStat(D)
            wl.cluster(method='sca', threshold=threshold, ref=ref)
        else:
            wl.add_entries('cog', 'concept,form', lambda x, y: x[y[0]] + '-' + x[y[1]])
            wl.renumber('cog', ref)
        return wl

    def get_profile(self, clts=None, filename=None):
        """
        Compute an orthography profile with LingPy's function.

        :param filename: Write the computed profile to a file in addition to returning it.
        :return: `segments.Profile` instance.
        """
        clts = clts.bipa if clts else None

        D = {0: ['doculect', 'concept', 'ipa']}
        for i, key in enumerate(self._concordances.form, start=1):
            D[i] = ['dummy', key[1], key[0]]
        wordlist = lingpy.basic.wordlist.Wordlist(D)

        if not filename:
            with tempfile.NamedTemporaryFile(delete=FileExistsError) as fp:
                pass
            p = pathlib.Path(fp.name)
        else:
            p = pathlib.Path(filename)

        with UnicodeWriter(p, delimiter='\t') as w:
            w.writerow(['Grapheme', 'IPA', 'Example', 'Count', 'Unicode'])
            for line in lingpy.sequence.profile.context_profile(wordlist, ref='ipa', clts=clts):
                w.writerow([line[0], line[1], line[2], line[4], line[5]])

        res = segments.Profile.from_file(p)
        if not filename:
            p.unlink()
        return res

    def write_app(self, dest='app'):
        # idxs must be in index 2 of wordlist, form 0, and concept 1
        # concordance 0 is phrase, 1 is gloss

        wordlist = self.get_wordlist()
        WL, CN = collections.OrderedDict(), collections.OrderedDict()
        for idx, form, concept, refs in wordlist.iter_rows('form', 'concept', 'references'):
            WL[idx] = [
                form,
                concept,
                [[int(y) for y in x.split(':')] for x in refs.split()],
                wordlist[idx, 'tokens'],
            ]

            for line in WL[idx][2]:
                igt = self[str(line[0])]
                CN[line[0]] = [
                    igt.phrase,
                    igt.gloss,
                ]
                # FIXME: must add additional IGT data from ExampleTable row!
        dest = pathlib.Path(dest)
        assert dest.is_dir()
        with dest.joinpath('script.js').open('w', encoding='utf8') as f:
            f.write('var WORDLIST = ' + json.dumps(WL, indent=2) + ';\n')
            f.write('var CONC = ' + json.dumps(CN, indent=2) + ';\n')
        index = dest / 'index.html'
        if not index.exists():
            shutil.copy(str(pathlib.Path(__file__).parent.joinpath('index.html')), str(index))
