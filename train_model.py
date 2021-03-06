import json
import os

import chainer
from chainer import training
from chainer.datasets import TransformDataset
from chainer.training import extensions
from chainer_chemistry.datasets import NumpyTupleDataset
from chainer.dataset import iterator as iterator_module, convert

import argparser
from data import transform_zinc250k
from data.transform_zinc250k import transform_fn_zinc250k, zinc250_atomic_num_list
# from data.utils import transform_fn_qm9
from generate import generate_mols
from graph_nvp.hyperparams import Hyperparameters
from graph_nvp.models.model import GraphNvpModel
from graph_nvp.utils import check_validity, save_mol_png


class MolNvpUpdater(training.StandardUpdater):
    def __init__(self, iterator, opt, device, loss_func,
                 converter=convert.concat_examples):
        super(MolNvpUpdater, self).__init__(
            iterator=iterator,
            optimizer=opt,
            converter=converter,
            loss_func=loss_func,
            device=device,
            loss_scale=None,
        )
        if isinstance(iterator, iterator_module.Iterator):
            iterator = {'main': iterator}
        self.iterator = iterator
        self.opt = opt
        self.device = device
        self.loss_func = loss_func
        self.model = opt.target
        self.converter = converter

    def update_core(self):
        two_step = True
        batch = self._iterators['main'].next()
        in_arrays = self.converter(batch, self.device)
        x = in_arrays[0]
        z, sum_log_det_jacs = self.model(in_arrays[1], x)
        optimizer = self._optimizers['main']
        nll = self.model.log_prob(z, sum_log_det_jacs)
        if two_step:
            alpha = 1.
            loss = (nll[0] + alpha * nll[1]) / (1. + alpha)
            chainer.reporter.report({'log_likelihood': loss, 'nll_x': nll[0],
                                     'nll_adj': nll[1]})
        else:
            loss = nll
            chainer.reporter.report({'log_likelihood': loss})
        self.model.cleargrads()
        loss.backward()
        optimizer.update()


def train():
    parser = argparser.get_parser()
    args = parser.parse_args()

    device = -1
    if args.gpu >= 0:
        device = args.gpu
    debug = args.debug
    print('input args:\n', json.dumps(vars(args), indent=4, separators=(',', ':')))  # pretty print args

    if args.data_name == 'qm9':
        from data import transform_qm9
        transform_fn = transform_qm9.transform_fn
        atomic_num_list = [6, 7, 8, 9, 0]
        mlp_channels = [256, 256]
        gnn_channels = {'gcn': [8, 64], 'hidden': [128, 64]}
        valid_idx = transform_qm9.get_val_ids()
    elif args.data_name == 'zinc250k':
        transform_fn = transform_fn_zinc250k
        atomic_num_list = zinc250_atomic_num_list
        mlp_channels = [1024, 512]
        gnn_channels = {'gcn': [16, 128], 'hidden': [256, 64]}
        valid_idx = transform_zinc250k.get_val_ids()

    dataset = NumpyTupleDataset.load(os.path.join(args.data_dir, args.data_file))
    dataset = TransformDataset(dataset, transform_fn)

    if len(valid_idx) > 0:
        train_idx = [t for t in range(len(dataset)) if t not in valid_idx]
        n_train = len(train_idx)
        train_idx.extend(valid_idx)
        train, test = chainer.datasets.split_dataset(dataset, n_train, train_idx)
    else:
        train, test = chainer.datasets.split_dataset_random(dataset, int(len(dataset) * 0.8), seed=args.seed)

    train_iter = chainer.iterators.SerialIterator(train, args.batch_size)
    num_masks = {'node': args.num_node_masks, 'channel': args.num_channel_masks}
    mask_size = {'node': args.node_mask_size, 'channel': args.channel_mask_size}
    num_coupling = {'node': args.num_node_coupling, 'channel': args.num_channel_coupling}
    model_params = Hyperparameters(args.num_atoms, args.num_rels, len(atomic_num_list),
                                   num_masks=num_masks, mask_size=mask_size, num_coupling=num_coupling,
                                   batch_norm=args.apply_batch_norm,
                                   additive_transformations=args.additive_transformations,
                                   learn_dist=args.learn_dist,
                                   mlp_channels=mlp_channels,
                                   gnn_channels=gnn_channels
                                   )

    model = GraphNvpModel(model_params)

    if device >= 0:
        chainer.cuda.get_device(device).use()
        model.to_gpu(device)

    print('==========================================')
    if device >= 0:
        print('Using GPUs')
    print('Num Minibatch-size: {}'.format(args.batch_size))
    print('Num epoch: {}'.format(args.max_epochs))
    print('==========================================')
    os.makedirs(args.save_dir, exist_ok=True)
    model.save_hyperparams(os.path.join(args.save_dir, 'graphnvp-params.json'))

    opt = chainer.optimizers.Adam()
    opt.setup(model)
    updater = MolNvpUpdater(train_iter, opt, device=device, loss_func=None)
    trainer = training.Trainer(updater, (args.max_epochs, 'epoch'), out=args.save_dir)

    # trainer.extend(extensions.dump_graph('log_likelihood'))

    def print_validity(t):
        adj, x = generate_mols(model, batch_size=100, gpu=device)
        valid_mols = check_validity(adj, x, atomic_num_list, device)['valid_mols']
        mol_dir = os.path.join(args.save_dir, 'generated_{}'.format(t.updater.epoch))
        # mol_dir = os.path.join(args.save_dir, 'generated_{}'.format(t.updater.iteration))
        os.makedirs(mol_dir, exist_ok=True)
        for ind, mol in enumerate(valid_mols):
            save_mol_png(mol, os.path.join(mol_dir, '{}.png'.format(ind)))

    if debug:
        # trainer.extend(print_validity, trigger=(1, 'epoch'))
        trainer.extend(print_validity, trigger=(100, 'iteration'))
    save_epochs = args.save_epochs
    if save_epochs == -1:
        save_epochs = args.max_epochs

    trainer.extend(extensions.snapshot(), trigger=(save_epochs, 'epoch'))
    # trainer.extend(extensions.PlotReport(['log_likelihood'], 'epoch', file_name='qm9.png'),
    #                trigger=(100, 'iteration'))
    trainer.extend(extensions.PrintReport([
        'epoch', 'log_likelihood', 'nll_x', 'nll_adj', 'elapsed_time']))
    trainer.extend(extensions.LogReport())
    trainer.extend(extensions.ProgressBar())
    if args.load_params == 1:
        chainer.serializers.load_npz(args.load_snapshot, trainer)
    trainer.run()
    chainer.serializers.save_npz(os.path.join(args.save_dir, 'graph-nvp-final.npz'), model)


if __name__ == '__main__':
    train()
