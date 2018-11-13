"""
Module containing the models used for automatic diacritization.
"""
import re
from random import shuffle, sample

import numpy as np
import tensorflow.keras.backend as K
from tensorflow.keras import Model
from tensorflow.keras import losses
from tensorflow.keras import metrics
from tensorflow.keras import optimizers
from tensorflow.keras import utils
from tensorflow.keras.layers import LSTM, Dense, Lambda, Input, Bidirectional

from dataset_preprocessing import keep_selected_diacritics, NAME2DIACRITIC, clear_diacritics, extract_diacritics, \
    text_to_indices, CHAR2INDEX, ARABIC_DIACRITICS, read_text_file, filter_tokenized_sentence, \
    fix_double_diacritics_error, add_time_steps, input_to_sentence, tokenize, merge_diacritics

LAST_DIACRITIC_REGEXP = re.compile('['+''.join(ARABIC_DIACRITICS)+r']+(?= |$)')
TIME_STEPS = 7
OPTIMIZER = optimizers.RMSprop()


def generate_morphological_diacritics_dataset(sentences):
    """
    Generate a dataset for training on non ending diacritics only.
    :param sentences: list of str, the sentences.
    :return: list of input arrays and list of target arrays, each element is a batch.
    """
    harakat = set(NAME2DIACRITIC[x] for x in ('Fatha', 'Damma', 'Kasra', 'Sukun'))
    targets = [LAST_DIACRITIC_REGEXP.sub('', keep_selected_diacritics(s, harakat)) for s in sentences]
    input_array = []
    target_array = []
    for target in targets:
        u_target = clear_diacritics(target)
        harakat_labels = extract_diacritics(target)
        target_labels = np.zeros((len(u_target)))
        target_labels[np.array(harakat_labels) == NAME2DIACRITIC['Fatha']] = 1
        target_labels[np.array(harakat_labels) == NAME2DIACRITIC['Damma']] = 2
        target_labels[np.array(harakat_labels) == NAME2DIACRITIC['Kasra']] = 3
        target_labels[np.array(harakat_labels) == NAME2DIACRITIC['Sukun']] = 4
        input_array.append(text_to_indices(u_target))
        target_array.append(target_labels)
    return input_array, target_array


def generate_shadda_dataset(sentences):
    """
    Generate a dataset for training on shadda only.
    :param sentences: list of str, the sentences.
    :return: list of input arrays and list of target arrays, each element is a batch.
    """
    targets = [keep_selected_diacritics(s, {NAME2DIACRITIC['Shadda']}) for s in sentences if
               NAME2DIACRITIC['Shadda'] in s]
    input_array = []
    target_array = []
    for target in targets:
        u_target = clear_diacritics(target)
        only_shadda_labels = extract_diacritics(target)
        target_labels = np.zeros((len(u_target)))
        target_labels[np.array(only_shadda_labels) == NAME2DIACRITIC['Shadda']] = 1
        input_array.append(text_to_indices(u_target))
        target_array.append(target_labels)
    return input_array, target_array


def keras_precision(y_true, y_pred):
    """Precision metric.
    Only computes a batch-wise average of precision. Computes the precision, a
    metric for multi-label classification of how many selected items are
    relevant.
    """
    true_positives = K.sum(K.round(K.clip(y_true * y_pred, 0, 1)))
    predicted_positives = K.sum(K.round(K.clip(y_pred, 0, 1)))
    precision = true_positives / (predicted_positives + K.epsilon())
    return precision


def keras_recall(y_true, y_pred):
    """Recall metric.
    Only computes a batch-wise average of recall. Computes the recall, a metric
    for multi-label classification of how many relevant items are selected.
    """
    true_positives = K.sum(K.round(K.clip(y_true * y_pred, 0, 1)))
    possible_positives = K.sum(K.round(K.clip(y_true, 0, 1)))
    recall = true_positives / (possible_positives + K.epsilon())
    return recall


def shadda_post_corrections(in_out):
    """
    Correct any obviously misplaced shadda marks according to the character and its context.
    :param in_out: input layer and prediction layer outputs.
    :return: corrected predictions.
    """
    inputs, predictions = in_out
    # Drop the shadda from the forbidden letters
    forbidden_chars = [CHAR2INDEX[' '], CHAR2INDEX['ا'], CHAR2INDEX['ء'], CHAR2INDEX['أ'], CHAR2INDEX['إ'],
                       CHAR2INDEX['آ'], CHAR2INDEX['ى'], CHAR2INDEX['ئ'], CHAR2INDEX['ة'], CHAR2INDEX['0']]
    char_index = K.argmax(inputs[:, -1], axis=-1)
    allowed_instances = K.cast(K.not_equal(char_index, forbidden_chars[0]), 'float32')
    for char in forbidden_chars[1:]:
        allowed_instances *= K.cast(K.not_equal(char_index, char), 'float32')
    allowed_instances *= K.sum(inputs[:, -2], axis=-1)
    # Drop the shadda from the letter following the space
    previous_char_index = K.argmax(inputs[:, -2], axis=-1)
    allowed_instances *= K.cast(K.not_equal(previous_char_index, CHAR2INDEX[' ']), 'float32')
    return K.reshape(allowed_instances, (-1, 1)) * predictions


def train_shadda_model(train_sentences, test_sentences, epochs=20, show_predictions_count=10):
    shadda_count = 0
    total = 0
    for sentence in train_sentences:
        total += len(clear_diacritics(sentence))
        shadda_count += len(list(filter(lambda x: x == NAME2DIACRITIC['Shadda'], sentence)))
    balancing_factor = total / shadda_count
    print('Balancing factor = {:.5f}'.format(balancing_factor))
    print('Generating train dataset...')
    train_inputs, train_targets = generate_shadda_dataset(train_sentences)
    print('Generating test dataset...')
    test_inputs, test_targets = generate_shadda_dataset(test_sentences)
    print('Training...')
    input_layer = Input(shape=(TIME_STEPS, len(CHAR2INDEX)))
    lstm1_layer = Bidirectional(LSTM(100, dropout=0.2, return_sequences=True))(input_layer)
    lstm2_layer = Bidirectional(LSTM(100, dropout=0.2))(lstm1_layer)
    dense_layer = Dense(1, activation='sigmoid')(lstm2_layer)
    post_layer = Lambda(shadda_post_corrections)([input_layer, dense_layer])
    model = Model(inputs=input_layer, outputs=post_layer)
    model.compile(OPTIMIZER, losses.binary_crossentropy, [metrics.binary_accuracy, keras_precision, keras_recall])
    for i in range(1, epochs + 1):
        acc = 0
        loss = 0
        prec = 0
        rec = 0
        for k in range(len(train_targets)):
            l, a, p, r = model.train_on_batch(
                add_time_steps(utils.to_categorical(train_inputs[k], len(CHAR2INDEX)), TIME_STEPS),
                train_targets[k], class_weight={0: 1, 1: balancing_factor}
            )
            acc += a
            loss += l
            prec += p
            rec += r
            if k % 1000 == 0:
                print('{}/{}: Train ({}/{}):'.format(i, epochs, k + 1, len(train_targets)))
                print('Loss = {:.5f} | Accuracy = {:.2%} | Precision = {:.2%} | Recall = {:.2%}'.format(
                    loss / (k + 1), acc / (k + 1), prec / (k + 1), rec / (k + 1))
                )
        print('{}/{}: Test:'.format(i, epochs))
        acc = 0
        loss = 0
        prec = 0
        rec = 0
        for k in range(len(test_targets)):
            l, a, p, r = model.test_on_batch(
                add_time_steps(utils.to_categorical(test_inputs[k], len(CHAR2INDEX)), TIME_STEPS), test_targets[k]
            )
            acc += a
            loss += l
            prec += p
            rec += r
        print('Loss = {:.5f} | Accuracy = {:.2%} | Precision = {:.2%} | Recall = {:.2%}'.format(
            loss / len(test_targets), acc / len(test_targets), prec / len(test_targets), rec / len(test_targets))
        )
        print('Test predictions samples:')
        for k in sample(range(len(test_targets)), show_predictions_count):
            test_input = add_time_steps(utils.to_categorical(test_inputs[k], len(CHAR2INDEX)), TIME_STEPS)
            predicted_indices = model.predict_on_batch(test_input) >= 0.5
            u_text = input_to_sentence(test_input)
            diacritics = [NAME2DIACRITIC['Shadda'] if c else '' for c in predicted_indices]
            print(merge_diacritics(u_text, diacritics))


def morphological_diacritics_post_corrections(in_out):
    """
    Correct any obviously misplaced morphological diacritics marks according to the character and its context.
    :param in_out: input layer and prediction layer outputs.
    :return: corrected predictions.
    """
    inputs, predictions = in_out
    # Drop diacritics from the forbidden letters
    forbidden_chars = [CHAR2INDEX[' '], CHAR2INDEX['آ'], CHAR2INDEX['ى'], CHAR2INDEX['0']]
    char_index = K.argmax(inputs[:, -1], axis=-1)
    mask = K.cast(K.not_equal(char_index, forbidden_chars[0]), 'float32')
    for forbidden_char in forbidden_chars[1:]:
        mask *= K.cast(K.not_equal(char_index, forbidden_char), 'float32')
    mask = K.reshape(mask, (-1, 1))
    predictions = mask * predictions + (1 - mask) * K.one_hot(0, K.int_shape(predictions)[-1])
    # Force the correct diacritics before some long vowels
    f_prev_diac_chars = {CHAR2INDEX['ا']: 1, CHAR2INDEX['ى']: 1}
    prev_char_index = K.argmax(inputs[:, -2], axis=-1)
    for fd_char, f_diac in f_prev_diac_chars.items():
        mask = K.clip(K.cast(K.not_equal(char_index[1:], fd_char), 'float32') +
                      K.cast(K.equal(prev_char_index[1:], CHAR2INDEX[' ']), 'float32'), 0, 1)
        mask = K.reshape(K.concatenate([mask, K.ones((1,))], axis=0), (-1, 1))
        predictions = predictions * mask + (1 - mask) * K.one_hot(f_diac, K.int_shape(predictions)[-1])
    # Drop the last diacritic of every word
    mask = K.reshape(K.concatenate([K.cast(K.not_equal(char_index[1:], CHAR2INDEX[' ']), 'float32'), K.zeros((1,))],
                                   axis=0), (-1, 1))
    predictions = mask * predictions + (1 - mask) * K.one_hot(0, K.int_shape(predictions)[-1])
    return predictions


def train_morphological_diacritics_model(train_sentences, test_sentences, epochs=20, show_predictions_count=10):
    b_factors = np.zeros((5,))
    for sentence in train_sentences:
        b_factors[0] += len(clear_diacritics(sentence))
        b_factors[1] += sentence.count(NAME2DIACRITIC['Fatha'])
        b_factors[2] += sentence.count(NAME2DIACRITIC['Damma'])
        b_factors[3] += sentence.count(NAME2DIACRITIC['Kasra'])
        b_factors[4] += sentence.count(NAME2DIACRITIC['Sukun'])
    b_factors[0] = b_factors[0] - np.sum(b_factors[1:])
    b_factors = np.max(b_factors) / b_factors
    print('Balancing factors: None={:.5f} Fatha={:.5f} Damma={:.5f} Kasra={:.5f} Sukun={:.5f}'.
          format(*b_factors))
    print('Generating train dataset...')
    train_inputs, train_targets = generate_morphological_diacritics_dataset(train_sentences)
    print('Generating test dataset...')
    test_inputs, test_targets = generate_morphological_diacritics_dataset(test_sentences)
    print('Training...')
    input_layer = Input(shape=(TIME_STEPS, len(CHAR2INDEX)))
    lstm1_layer = Bidirectional(LSTM(100, dropout=0.2, return_sequences=True))(input_layer)
    lstm2_layer = Bidirectional(LSTM(100, dropout=0.2))(lstm1_layer)
    dense_layer = Dense(len(b_factors), activation='softmax')(lstm2_layer)
    post_layer = Lambda(morphological_diacritics_post_corrections)([input_layer, dense_layer])
    model = Model(inputs=input_layer, outputs=post_layer)
    model.compile(OPTIMIZER, losses.categorical_crossentropy, [metrics.categorical_accuracy, keras_precision,
                                                               keras_recall])
    for i in range(1, epochs + 1):
        acc = 0
        loss = 0
        prec = 0
        rec = 0
        for k in range(len(train_targets)):
            l, a, p, r = model.train_on_batch(
                add_time_steps(utils.to_categorical(train_inputs[k], len(CHAR2INDEX)), TIME_STEPS),
                utils.to_categorical(train_targets[k], len(b_factors)), class_weight=dict(enumerate(b_factors))
            )
            acc += a
            loss += l
            prec += p
            rec += r
            if k % 1000 == 0:
                print('{}/{}: Train ({}/{}):'.format(i, epochs, k + 1, len(train_targets)))
                print('Loss = {:.5f} | Accuracy = {:.2%} | Precision = {:.2%} | Recall = {:.2%}'.format(
                    loss / (k + 1), acc / (k + 1), prec / (k + 1), rec / (k + 1))
                )
        print('{}/{}: Test:'.format(i, epochs))
        acc = 0
        loss = 0
        prec = 0
        rec = 0
        for k in range(len(test_targets)):
            l, a, p, r = model.test_on_batch(
                add_time_steps(utils.to_categorical(test_inputs[k], len(CHAR2INDEX)), TIME_STEPS),
                utils.to_categorical(test_targets[k], len(b_factors))
            )
            acc += a
            loss += l
            prec += p
            rec += r
        print('Loss = {:.5f} | Accuracy = {:.2%} | Precision = {:.2%} | Recall = {:.2%}'.format(
            loss / len(test_targets), acc / len(test_targets), prec / len(test_targets), rec / len(test_targets))
        )
        print('Test predictions samples:')
        for k in sample(range(len(test_targets)), show_predictions_count):
            test_input = add_time_steps(utils.to_categorical(test_inputs[k], len(CHAR2INDEX)), TIME_STEPS)
            predicted_indices = np.argmax(model.predict_on_batch(test_input), axis=-1)
            u_text = input_to_sentence(test_input)
            diacritics = np.empty((len(u_text),), dtype=str)
            diacritics[predicted_indices == 0] = ''
            diacritics[predicted_indices == 1] = NAME2DIACRITIC['Fatha']
            diacritics[predicted_indices == 2] = NAME2DIACRITIC['Damma']
            diacritics[predicted_indices == 3] = NAME2DIACRITIC['Kasra']
            diacritics[predicted_indices == 4] = NAME2DIACRITIC['Sukun']
            print(merge_diacritics(u_text, diacritics.tolist()))


if __name__ == '__main__':

    file_paths = [
        # r'D:\Data\Documents\Tashkeela-arabic-diacritized-text-utf8-0.3\texts.txt\إتحاف المهرة لابن حجر.txt',
        # r'D:\Data\Documents\Tashkeela-arabic-diacritized-text-utf8-0.3\texts.txt\أحكام القرآن لابن العربي.txt',
        r'D:\Data\Documents\Tashkeela-arabic-diacritized-text-utf8-0.3\texts.txt\أدب الدنيا والدين.txt',
        r'D:\Data\Documents\Tashkeela-arabic-diacritized-text-utf8-0.3\texts.txt\الأحكام السلطانية.txt'
    ]
    sentences = []
    print('Loading text...')
    for file_path in file_paths:
        sentences += read_text_file(file_path)
    print('Parsing and cleaning...')
    sentences = [' '.join(sf) for sf in
                 filter(lambda x: len(x) > 0, [filter_tokenized_sentence(tokenize(fix_double_diacritics_error(s)))
                                               for s in sentences])]
    shuffle(sentences)
    print('Number of sentences =', len(sentences))
    train_size = round(0.9 * len(sentences))
    train_sentences = sentences[:train_size]
    test_sentences = sentences[train_size:]
    print('In train =', len(train_sentences))
    print('In test =', len(test_sentences))
    # train_shadda_model(train_sentences, test_sentences, 10)
    train_morphological_diacritics_model(train_sentences, test_sentences, 10)
