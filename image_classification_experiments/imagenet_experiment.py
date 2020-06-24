import argparse
import torch
# import numpy as np
import json
import os
# import image_classification_experiments.utils_imagenet as utils_imagenet
import image_classification_experiments.utils as utils
from image_classification_experiments.REMINDModel import REMINDModel
from image_classification_experiments.imagenet_base_initialization import *

torch.multiprocessing.set_sharing_strategy('file_system')


def get_data_loader(args, split, min_class, max_class, batch_size=128, return_item_ix=False, shuffle=None, sampler=None,
                    batch_sampler=None):
    training = split == 'train'
    if shuffle is None:
        shuffle = training

    data_loader = utils_imagenet.get_imagenet_data_loader(args.images_dir + '/' + split, args.label_dir, split,
                                                          shuffle=shuffle, min_class=min_class, max_class=max_class,
                                                          batch_size=batch_size,
                                                          return_item_ix=return_item_ix, sampler=sampler,
                                                          batch_sampler=batch_sampler, dataset_name='imagenet',
                                                          augment=args.augment,
                                                          augmentation_techniques=args.augmentation_techniques)
    return data_loader


def compute_accuracies(loader, remind, pq):
    _, probas, y_test_init = remind.predict(loader, pq)
    top1, top5 = utils.accuracy(probas, y_test_init, topk=(1, 5))
    return probas, top1, top5


def update_accuracies(args, curr_max_class, remind, pq, accuracies):
    base_test_loader = get_data_loader(args, split='val', min_class=args.initial_min_class,
                                       max_class=args.initial_max_class)
    base_probas, base_top1, base_top5 = compute_accuracies(base_test_loader, remind, pq)
    print('\nBase Classes (%d-%d): top1=%0.2f%% -- top5=%0.2f%%' % (
        args.initial_min_class, args.initial_max_class, base_top1, base_top5))

    seen_classes_test_loader = get_data_loader(args, 'val', args.initial_min_class, curr_max_class)
    seen_probas, seen_top1, seen_top5 = compute_accuracies(seen_classes_test_loader, remind, pq)
    print('Seen Classes (%d-%d): top1=%0.2f%% -- top5=%0.2f%%' % (
        args.initial_min_class, curr_max_class - 1, seen_top1, seen_top5))

    non_base_classes_test_loader = get_data_loader(args, 'val', args.initial_max_class, curr_max_class)
    non_base_probas, non_base_top1, non_base_top5 = compute_accuracies(non_base_classes_test_loader, remind, pq)

    print('Non-base Classes (%d-%d): top1=%0.2f%% -- top5=%0.2f%%' % (
        args.initial_max_class, curr_max_class - 1, non_base_top1, non_base_top5))

    accuracies['base_classes_top1'].append(float(base_top1))
    accuracies['base_classes_top5'].append(float(base_top5))
    accuracies['non_base_classes_top1'].append(float(non_base_top1))
    accuracies['non_base_classes_top5'].append(float(non_base_top5))
    accuracies['seen_classes_top1'].append(float(seen_top1))
    accuracies['seen_classes_top5'].append(float(seen_top5))

    utils.save_accuracies(accuracies, min_class_trained=args.initial_min_class, max_class_trained=curr_max_class,
                          save_path=args.save_dir)
    utils.save_predictions(seen_probas, args.initial_min_class, curr_max_class - 1, args.save_dir)
    print("\n\n")


def streaming(args, remind):
    accuracies = {'base_classes_top1': [], 'non_base_classes_top1': [], 'seen_classes_top1': [],
                  'base_classes_top5': [], 'non_base_classes_top5': [], 'seen_classes_top5': []}

    counter = utils.Counter()
    print('\nPerforming base initialization...')
    feat_data, label_data, item_ix_data = extract_base_init_features(args.images_dir, args.label_dir,
                                                                     args.extract_features_from,
                                                                     args.classifier_ckpt,
                                                                     args.base_arch, args.initial_max_class,
                                                                     args.num_channels,
                                                                     args.spatial_feat_dim)
    pq, latent_dict, rehearsal_ixs, class_id_to_item_ix_dict = fit_pq(feat_data, label_data, item_ix_data,
                                                                      args.num_channels,
                                                                      args.spatial_feat_dim, args.num_codebooks,
                                                                      args.codebook_size, counter=counter)

    initial_test_loader = get_data_loader(args, split='val', min_class=args.initial_min_class,
                                          max_class=args.initial_max_class)
    print('\nComputing base accuracies...')
    base_probas, base_top1, base_top5 = compute_accuracies(initial_test_loader, remind, pq)

    print('\nInitial Test: top1=%0.2f%% -- top5=%0.2f%%' % (base_top1, base_top5))
    utils.save_predictions(base_probas, args.initial_min_class, args.initial_max_class - 1, args.save_dir)
    accuracies['base_classes_top1'].append(float(base_top1))
    accuracies['base_classes_top5'].append(float(base_top5))
    accuracies['seen_classes_top1'].append(float(base_top1))
    accuracies['seen_classes_top5'].append(float(base_top5))

    print('\nBeginning streaming training...')
    for class_ix in range(args.streaming_min_class, args.streaming_max_class, args.class_increment):
        max_class = class_ix + args.class_increment
        train_loader_curr = get_data_loader(args, 'train', class_ix, max_class, batch_size=args.batch_size,
                                            return_item_ix=True, shuffle=False)
        test_loader = get_data_loader(args, 'val', args.min_class, max_class, batch_size=args.batch_size)

        # fit model with rehearsal
        remind.fit_incremental_batch(train_loader_curr, latent_dict, pq, rehearsal_ixs=rehearsal_ixs,
                                     class_id_to_item_ix_dict=class_id_to_item_ix_dict,
                                     counter=counter)

        _, probas, y_test = remind.predict(test_loader, pq)
        update_accuracies(args, curr_max_class=max_class, remind=remind, pq=pq, accuracies=accuracies)

    # final accuracy
    test_loader = get_data_loader(args, 'val', args.min_class, args.streaming_max_class, batch_size=args.batch_size)
    _, probas, y_test = remind.predict(test_loader, pq)
    top1, top5 = utils.accuracy(probas, y_test, topk=(1, 5))
    print('\nFinal: top1=%0.2f%% -- top5=%0.2f%%' % (top1, top5))


def get_not_none(arg1, arg2):
    if arg1 is not None:
        return arg1
    else:
        return arg2


def fix_args(args):
    args.classifier = get_not_none(args.classifier, 'ResNet18_StartAt_Layer4_1')
    args.initial_max_class = get_not_none(args.initial_max_class, 100)
    args.num_classes = get_not_none(args.num_classes, 1000)
    args.class_increment = get_not_none(args.class_increment, 100)
    args.streaming_min_class = get_not_none(args.streaming_min_class, 100)
    args.streaming_max_class = get_not_none(args.streaming_max_class, 1000)
    args.overall_max_class = get_not_none(args.overall_max_class, 1000)
    if args.save_dir is None:
        args.save_dir = 'streaming_experiments/' + args.expt_name

    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    if args.classifier_ckpt == 'None':
        args.classifier_ckpt = None

    if args.lr_mode == 'step_lr_per_class':
        args.lr_gamma = np.exp(args.lr_step_size * np.log(args.end_lr / args.start_lr) / 1300)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    # change these
    parser.add_argument('--classifier', type=str, default=None)
    parser.add_argument('--classifier_ckpt', type=str)
    parser.add_argument('--expt_name', type=str)
    parser.add_argument('--label_dir', type=str, default=None)
    parser.add_argument('--images_dir', type=str, default=None)
    parser.add_argument('--save_dir', type=str, required=False)

    # new params
    parser.add_argument('--extract_features_from', type=str, default='model.layer4.0')
    parser.add_argument('--base_arch', type=str, default='ResNet18ClassifyAfterLayer4_1')
    parser.add_argument('--num_channels', type=int, default=512)
    parser.add_argument('--spatial_feat_dim', type=int, default=7)
    parser.add_argument('--num_codebooks', type=int, default=32)
    parser.add_argument('--codebook_size', type=int, default=256)
    parser.add_argument('--max_buffer_size', type=int, default=None)

    # learning rate parameters
    parser.add_argument('--lr_mode', type=str, choices=['step_lr_per_class'])
    parser.add_argument('--lr_step_size', type=int, default=None)
    parser.add_argument('--start_lr', type=float, default=0.1)
    parser.add_argument('--end_lr', type=float)
    parser.add_argument('--lr_gamma', type=float, default=0.5)

    # augmentation parameters
    parser.add_argument('--augment', action='store_true')
    parser.add_argument('--augmentation_techniques', type=str, nargs='+', default=['crop', 'flip'])
    parser.add_argument('--random_resized_crops', action='store_true')
    parser.add_argument('--use_mixup', action='store_true')
    parser.add_argument('--mixup_alpha', type=float, default=0.2)

    # probably no need to change these
    parser.add_argument('--weight_decay', type=float, default=1e-5)
    parser.add_argument('--min_class', type=int, default=0)
    parser.add_argument('--initial_min_class', type=int, default=0)
    parser.add_argument('--initial_max_class', type=int, default=None)
    parser.add_argument('--num_classes', type=int, default=None)
    parser.add_argument('--class_increment', type=int, default=None)
    parser.add_argument('--streaming_min_class', type=int, default=None)
    parser.add_argument('--streaming_max_class', type=int, default=None)
    parser.add_argument('--overall_max_class', type=int, default=None)
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--rehearsal_samples', type=int, default=50)

    # get arguments and print them out
    args = parser.parse_args()
    fix_args(args)
    print("Arguments {}".format(json.dumps(vars(args), indent=4, sort_keys=True)))

    # make model and begin stream training
    remind = REMINDModel(num_classes=args.num_classes, classifier_G=args.base_arch,
                         extract_features_from=args.extract_features_from, classifier_F=args.classifier,
                         classifier_ckpt=args.classifier_ckpt,
                         weight_decay=args.weight_decay, lr_mode=args.lr_mode, lr_step_size=args.lr_step_size,
                         start_lr=args.start_lr, end_lr=args.end_lr, lr_gamma=args.lr_gamma,
                         num_samples=args.rehearsal_samples, use_mixup=args.use_mixup, mixup_alpha=args.mixup_alpha,
                         grad_clip=None, num_channels=args.num_channels, num_feats=args.spatial_feat_dim,
                         num_codebooks=args.num_codebooks, use_random_resize_crops=args.random_resized_crops,
                         max_buffer_size=args.max_buffer_size)
    streaming(args, remind)