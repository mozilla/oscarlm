
import os
import sys
import math
import struct
import shutil
import argparse
import itertools
import subprocess

from collections import Counter
from multiprocessing import Process, Queue
from languages import LANGUAGE_CODES, get_language
from utils import maybe_download, maybe_ungzip, maybe_join, section, log_progress, MEGABYTE, announce, parse_file_size

STOP_TOKEN = False
MAX_KEYS = 100000

SW_DIR = os.getenv('SW_DIR', 'dependencies')
KENLM_BIN = SW_DIR + '/kenlm/build/bin'
DEEPSPEECH_BIN = SW_DIR + '/deepspeech'


def get_partial_path(index):
    return os.path.join(LANG.model_dir, 'prepared.txt.partial{}'.format(index))


def count_words(index, counters):
    counter = Counter()
    unprepared_txt = os.path.join(LANG.model_dir, 'unprepared.txt')
    block_size = math.ceil(os.path.getsize(unprepared_txt) / ARGS.workers)
    start = index * block_size
    end = start + block_size
    with open(unprepared_txt, 'rb') as unprepared_file, open(get_partial_path(index), 'w') as partial_file:
        pos = old_pos = start
        unprepared_file.seek(start)
        while pos < end:
            try:
                announce('Shard {}: reading'.format(index))
                lines = unprepared_file.readlines(max(ARGS.block_size, end - pos))
                if index > 0 and pos == start:
                    lines = lines[1:]
                pos = unprepared_file.tell()
                announce('Shard {}: cleaning'.format(index))
                lines = list(itertools.chain.from_iterable(map(lambda l: LANG.clean(l.decode()), lines)))
                announce('Shard {}: counting'.format(index))
                for line in lines:
                    for word in line.split():
                        counter[word] += 1
                announce('Shard {}: writing'.format(index))
                partial_file.writelines(map(lambda l: l + '\n', lines))
                if len(counter.keys()) > MAX_KEYS or pos >= end:
                    announce('Shard {}: sending'.format(index))
                    counters.put((counter, pos - old_pos))
                    old_pos = pos
                    counter = Counter()
            except Exception as ex:
                announce('Preparation worker failed:' + str(ex))


def aggregate_counters(vocabulary_txt, source_bytes, counters):
    overall_counter = Counter()
    progress_indicator = log_progress(total=source_bytes / MEGABYTE, entity='MB', format=':8.2f')
    while True:
        counter_and_read_bytes = counters.get()
        if counter_and_read_bytes == STOP_TOKEN:
            with open(vocabulary_txt, 'w') as vocabulary_file:
                vocabulary_file.write('\n'.join(str(word) for word, count in overall_counter.most_common(ARGS.vocabulary_size)))
            progress_indicator.end()
            return
        counter, read_bytes = counter_and_read_bytes
        overall_counter += counter
        progress_indicator.increment(steps=read_bytes / MEGABYTE)
        if len(overall_counter.keys()) > ARGS.prune_factor * ARGS.vocabulary_size:
            overall_counter = Counter(overall_counter.most_common(ARGS.vocabulary_size))


def get_serialized_utf8_alphabet():
    res = bytearray()
    res += struct.pack('<h', 255)
    for i in range(255):
        # Note that we also shift back up in the mapping constructed here
        # so that the native client sees the correct byte values when decoding.
        res += struct.pack('<hh1s', i, 1, bytes([i+1]))
    return bytes(res)


def main():
    raw_txt_gz = os.path.join(LANG.model_dir, 'raw.txt.gz')
    unprepared_txt = os.path.join(LANG.model_dir, 'unprepared.txt')
    prepared_txt = os.path.join(LANG.model_dir, 'prepared.txt')
    vocabulary_txt = os.path.join(LANG.model_dir, 'vocabulary.txt')
    unfiltered_arpa = os.path.join(LANG.model_dir, 'unfiltered.arpa')
    filtered_arpa = os.path.join(LANG.model_dir, 'filtered.arpa')
    lm_binary = os.path.join(LANG.model_dir, 'lm.binary')
    kenlm_scorer = os.path.join(LANG.model_dir, 'kenlm.scorer')
    temp_prefix = os.path.join(LANG.model_dir, 'tmp')

    redo = ARGS.force_download

    section('Downloading text data', empty_lines_before=1)
    redo = maybe_download(LANG.text_url, raw_txt_gz, force=redo)

    section('Unzipping text data')
    redo = maybe_ungzip(raw_txt_gz, unprepared_txt, force=redo)

    redo = redo or ARGS.force_generate

    section('Preparing text and building vocabulary')
    if redo or not os.path.isfile(prepared_txt) or not os.path.isfile(vocabulary_txt):
        redo = True
        announce('Preparing {} shards of "{}"...'.format(ARGS.workers, unprepared_txt))
        counters = Queue(ARGS.workers)
        source_bytes = os.path.getsize(unprepared_txt)
        aggregator_process = Process(target=aggregate_counters, args=(vocabulary_txt, source_bytes, counters))
        aggregator_process.start()
        counter_processes = list(map(lambda index: Process(target=count_words, args=(index, counters)),
                                     range(ARGS.workers)))
        try:
            for p in counter_processes:
                p.start()
            for p in counter_processes:
                p.join()
            counters.put(STOP_TOKEN)
            aggregator_process.join()
            partials = list(map(lambda i: get_partial_path(i), range(ARGS.workers)))
            maybe_join(partials, prepared_txt)
            for partial in partials:
                os.unlink(partial)
        except KeyboardInterrupt:
            aggregator_process.terminate()
            for p in counter_processes:
                p.terminate()
            raise
    else:
        announce('Files "{}" and \n\t"{}" existing - not preparing'.format(prepared_txt, vocabulary_txt))

    section('Building unfiltered language model')
    if redo or not os.path.isfile(unfiltered_arpa):
        redo = True
        subprocess.check_call([
            KENLM_BIN + '/lmplz',
            '--temp_prefix', temp_prefix,
            '--memory', '80%',
            '--discount_fallback',
            '--text', prepared_txt,
            '--arpa', unfiltered_arpa,
            '--skip', 'symbols',
            '--order', '5',
            '--prune', '0', '0', '1'
        ])
    else:
        announce('File "{}" existing - not generating'.format(unfiltered_arpa))

    section('Filtering language model')
    if redo or not os.path.isfile(filtered_arpa):
        redo = True
        with open(vocabulary_txt, 'rb') as vocabulary_file:
            vocabulary_content = vocabulary_file.read()
        subprocess.run([
            KENLM_BIN + '/filter',
            'single',
            'model:' + unfiltered_arpa,
            filtered_arpa
        ], input=vocabulary_content, check=True)
    else:
        announce('File "{}" existing - not filtering'.format(filtered_arpa))

    section('Generating binary representation')
    if redo or not os.path.isfile(lm_binary):
        redo = True
        subprocess.check_call([
            KENLM_BIN + '/build_binary',
            '-a', '255',
            '-q', '8',
            '-v',
            'trie',
            filtered_arpa,
            lm_binary
        ])
    else:
        announce('File "{}" existing - not generating'.format(lm_binary))

    section('Building scorer')
    if redo or not os.path.isfile(kenlm_scorer):
        redo = True
        words = set()
        vocab_looks_char_based = True
        with open(vocabulary_txt) as vocabulary_file:
            for line in vocabulary_file:
                for word in line.split():
                    words.add(word.encode())
                    if len(word) > 1:
                        vocab_looks_char_based = False
        announce("{} unique words read from vocabulary file.".format(len(words)))
        announce(
            "{} like a character based model.".format(
                "Looks" if vocab_looks_char_based else "Doesn't look"
            )
        )
        if ARGS.alphabet_mode == 'auto':
            use_utf8 = vocab_looks_char_based
        elif ARGS.alphabet_mode == 'utf8':
            use_utf8 = True
        else:
            use_utf8 = False
        serialized_alphabet = get_serialized_utf8_alphabet() if use_utf8 else LANG.get_serialized_alphabet()
        from ds_ctcdecoder import Scorer, Alphabet
        alphabet = Alphabet()
        err = alphabet.deserialize(serialized_alphabet, len(serialized_alphabet))
        if err != 0:
            announce('Error loading alphabet: {}'.format(err))
            sys.exit(1)
        scorer = Scorer()
        scorer.set_alphabet(alphabet)
        scorer.set_utf8_mode(use_utf8)
        scorer.reset_params(LANG.alpha, LANG.beta)
        scorer.load_lm(lm_binary)
        scorer.fill_dictionary(list(words))
        shutil.copy(lm_binary, kenlm_scorer)
        scorer.save_dictionary(kenlm_scorer, True)  # append, not overwrite
        announce('Package created in {}'.format(kenlm_scorer))
        announce('Testing package...')
        scorer = Scorer()
        scorer.load_lm(kenlm_scorer)
    else:
        announce('File "{}" existing - not building'.format(kenlm_scorer))


def parse_args():
    parser = argparse.ArgumentParser(description='Generate language models from OSCAR corpora', prog='genlm')
    parser.add_argument('language', choices=LANGUAGE_CODES,
                        help='language of the model to generate')
    parser.add_argument('--workers', type=int, default=os.cpu_count(),
                        help='number of preparation and counting workers')
    parser.add_argument('--block-size', type=str, default='100M',
                        help='(maximum) preparation block size per worker to read at once during preparation')
    parser.add_argument('--prune-factor', type=int, default=10,
                        help='times --vocabulary-size of items to keep in each vocabulary aggregator')
    parser.add_argument('--vocabulary-size', type=int, default=500000,
                        help='final number of words in vocabulary')
    parser.add_argument('--alpha', type=float, default=None,
                        help='overrides language-specific alpha parameter')
    parser.add_argument('--beta', type=float, default=None,
                        help='overrides language-specific beta parameter')
    parser.add_argument('--alphabet-mode', choices=['auto', 'utf8', 'specific'], default='auto',
                        help='if alphabet-mode should be determined from the vocabulary (auto), '
                             'or the alphabet should be all utf-8 characters (utf8), '
                             'or the alphabet should be language specific (specific)')
    parser.add_argument('--force-download', action='store_true',
                        help='forces re-downloading and re-generating from scratch')
    parser.add_argument('--force-generate', action='store_true',
                        help='forces re-generating from scratch (reusing available download)')
    return parser.parse_args()


if __name__ == '__main__':
    ARGS = parse_args()
    LANG = get_language(ARGS.language)
    if ARGS.alpha is not None:
        LANG.alpha = ARGS.alpha
    if ARGS.beta is not None:
        LANG.beta = ARGS.beta
    ARGS.block_size = parse_file_size(ARGS.block_size)
    try:
        main()
    except KeyboardInterrupt:
        announce('\nInterrupted')
        sys.exit()
