# -*- coding: utf-8 -*-
"""Building and simulating spiking neural networks using Brian2.

@author: rbodo
"""

from __future__ import division, absolute_import
from __future__ import print_function, unicode_literals

import warnings

import numpy as np
import os
from future import standard_library

from snntoolbox.simulation.utils import AbstractSNN

standard_library.install_aliases()


class SNN(AbstractSNN):
    """
    Represents the compiled spiking neural network, ready for testing in a
    spiking simulator.

    Attributes
    ----------

    layers: list[brian2.NeuronGroup]
        Each entry represents a layer, i.e. a population of neurons, in form of
        Brian2 ``NeuronGroup`` objects.

    connections: list[brian2.Synapses]
        Brian2 ``Synapses`` objects representing the connections between
        individual layers.

    threshold: str
        Defines spiking threshold.

    v_reset: str
        Defines reset potential.

    eqs: str
        Differential equation for membrane potential.

    spikemonitors: list[brian2.SpikeMonitor]
        Brian2 ``SpikeMonitor`` s for each layer that records spikes.

    statemonitors: list[brian2.StateMonitor]
        Brian2 ``StateMonitor`` s for each layer that records membrane
        potential.

    snn: brian2.Network
        The spiking network.
    """

    def __init__(self, config, queue=None):

        AbstractSNN.__init__(self, config, queue)

        self.layers = []
        self._conns = []  # Temporary container for layer connections.
        self._biases = []  # Temporary container for layer biases.
        self.connections = []  # Final container for all layers.
        self.threshold = 'v >= v_thresh'
        self.v_reset = 'v = v_reset'
        self.eqs = 'v = : 1'
        self.spikemonitors = []
        self.statemonitors = []
        self.snn = None
        self._input_layer = None
        self._cell_params = None

        # Track the output layer spikes.
        self.output_spikemonitor = None

    @property
    def is_parallelizable(self):
        return False

    def add_input_layer(self, input_shape):

        self.layers.append(self.sim.PoissonGroup(
            np.prod(input_shape[1:]), rates=0*self.sim.Hz,
            dt=self._dt*self.sim.ms))
        self.layers[0].add_attribute('label')
        self.layers[0].label = 'InputLayer'
        self.spikemonitors.append(self.sim.SpikeMonitor(self.layers[0]))
        # Need placeholders "None" for layers without states:
        self.statemonitors.append(self.sim.StateMonitor(self.layers[0], [],
                                                        False))

    def add_layer(self, layer):

        if 'Flatten' in layer.__class__.__name__:
            return

        self._conns = []
        self.layers.append(self.sim.NeuronGroup(
            np.prod(layer.output_shape[1:]), self.eqs, 'linear', self.threshold,
            self.v_reset, dt=self._dt*self.sim.ms))
        self.connections.append(self.sim.Synapses(
            self.layers[-2], self.layers[-1], 'w:1', on_pre='v+=w',
            dt=self._dt*self.sim.ms))
        self.layers[-1].add_attribute('label')
        self.layers[-1].label = layer.name
        if 'spiketrains' in self._plot_keys:
            self.spikemonitors.append(self.sim.SpikeMonitor(self.layers[-1]))
        if 'v_mem' in self._plot_keys:
            self.statemonitors.append(self.sim.StateMonitor(self.layers[-1],
                                                            'v', True))

    def build_dense(self, layer, input_weight=None):

        if layer.activation == 'softmax':
            raise warnings.warn("Activation 'softmax' not implemented. Using "
                                "'relu' activation instead.", RuntimeWarning)

        weights, self._biases = layer.get_weights()
        self.set_biases()
        self.connections[-1].connect(True)
        if input_weight is not None:
            self.connections[-1].w = input_weight.flatten()
        else:
            self.connections[-1].w = weights.flatten()
        print("Lenght of weights:{}".format(len(self.connections[-1].w)))

    def build_convolution(self, layer, input_weight=None):
        from snntoolbox.simulation.utils import build_convolution

        delay = self.config.getfloat('cell', 'delay')
        transpose_kernel = \
            self.config.get('simulation', 'keras_backend') == 'tensorflow'
        self._conns, self._biases = build_convolution(layer, delay,
                                                      transpose_kernel)
        self.set_biases()

        print("Connecting layer...")
        for conn in self._conns:
            i = conn[0]
            j = conn[1]
            self.connections[-1].connect(i=i, j=j)
        if input_weight is not None:
            self.connections[-1].w = input_weight.flatten()
        else:
            self.connections[-1].w[i, j] = conn[2]

    def build_pooling(self, layer, input_weight=None):
        from snntoolbox.simulation.utils import build_pooling

        delay = self.config.getfloat('cell', 'delay')
        self._conns = build_pooling(layer, delay)

        for conn in self._conns:
            self.connections[-1].connect(i=conn[0], j=conn[1])
            if input_weight is not None:
                self.connections[-1].w = input_weight.flatten()
            else:
                self.connections[-1].w = 1 / np.prod(layer.pool_size)

    def compile(self):

        self.output_spikemonitor = self.sim.SpikeMonitor(self.layers[-1])
        spikemonitors = self.spikemonitors + [self.output_spikemonitor]
        self.snn = self.sim.Network(self.layers, self.connections,
                                    spikemonitors, self.statemonitors)
        self.snn.store()

        # Set input layer
        for obj in self.snn.objects:
            if 'poissongroup' in obj.name and 'thresholder' not in obj.name:
                self._input_layer = obj
        assert self._input_layer, "No input layer found."

    def simulate(self, **kwargs):

        if self._poisson_input:
            self._input_layer.rates = kwargs[str('x_b_l')].flatten() * 1000 / \
                                      self.rescale_fac * self.sim.Hz
        elif self._dataset_format == 'aedat':
            # TODO: Implement by using brian2.SpikeGeneratorGroup.
            raise NotImplementedError
        else:
            try:
                # TODO: Implement constant input by using brian2.TimedArray.
                self._input_layer.current = kwargs[str('x_b_l')].flatten()
            except AttributeError:
                raise NotImplementedError

        self.snn.run(self._duration*self.sim.ms, namespace=self._cell_params,
                     report='stdout', report_period=10*self.sim.ms)

        output_b_l_t = self.get_recorded_vars(self.layers)

        return output_b_l_t

    def reset(self, sample_idx):
        mod = self.config.getint('simulation', 'reset_between_nth_sample')
        mod = mod if mod else sample_idx + 1
        if sample_idx % mod == 0:
            print("Resetting simulator...")
            self.snn.restore()

    def end_sim(self):

        pass

    def save(self, path, filename):
        from snntoolbox.utils.utils import confirm_overwrite
        
        print("Saving weights ...")
        for i,  connection in enumerate(self.connections):
            filepath = os.path.join(path, self.config.get('paths', 'filename_snn'), 'brian2-model', self.layers[i+1].label + '.npz')
            if self.config.getboolean('output', 'overwrite') or confirm_overwrite(filepath):
                directory = os.path.dirname(filepath)
                if not os.path.exists(directory):
                    os.makedirs(directory)
                print("Store weights of layer {} to file {}".format(self.layers[i+1].label, filepath))
                np.savez(filepath, self.connections[i].w)

    def load(self, path, filename):
        import keras
        from snntoolbox.parsing.utils import get_type
        from snntoolbox.simulation.utils import get_ann_ops
        
        dirpath = os.path.join(path, filename, 'brian2-model')
        npz_files = [f for f in sorted(os.listdir(dirpath)) if os.path.isfile(os.path.join(dirpath, f))]
        print("Loading spiking model...")

        self.parsed_model = keras.models.load_model(
                os.path.join(self.config.get('paths', 'path_wd'),
                             self.config.get('paths', 'filename_parsed_model') + '.h5'))
        self.num_classes = int(self.parsed_model.layers[-1].output_shape[-1])
        self.top_k = min(self.num_classes, self.config.getint('simulation',
                                                              'top_k'))

        # Get batch input shape
        batch_shape = list(self.parsed_model.layers[0].batch_input_shape)
        batch_shape[0] = self.batch_size
        if self.config.get('conversion', 'spike_code') == 'ttfs_dyn_thresh':
            batch_shape[0] *= 2

        self.add_input_layer(batch_shape)

        # Iterate over layers to create spiking neurons and connections.
        for layer, f in zip(self.parsed_model.layers[1:], npz_files):
            print("Building layer: {}".format(layer.name))
            self.add_layer(layer)
            layer_type = get_type(layer)
            filepath = os.path.join(dirpath, f)
            print("Using layer-weights stored in: {}".format(filepath))
            print("Loading stored weights...")
            input_file = np.load(filepath)
            input_weight = input_file['arr_0'] 
            if layer_type == 'Dense':
                self.build_dense(layer, input_weight=input_weight)
            elif layer_type == 'Conv2D':
                self.build_convolution(layer, input_weight=input_weight)
                if layer.data_format == 'channels_last':
                    self.data_format = layer.data_format
            elif layer_type in {'MaxPooling2D', 'AveragePooling2D'}:
                self.build_pooling(layer, input_weight=input_weight)
            elif layer_type == 'Flatten':
                self.build_flatten(layer)

        print("Compiling spiking model...\n")
        self.compile()

        # Compute number of operations of ANN.
        if self.fanout is None:
            self.set_connectivity()
            self.operations_ann = get_ann_ops(self.num_neurons,
                                              self.num_neurons_with_bias,
                                              self.fanin)
            print("Number of operations of ANN: {}".format(self.operations_ann))
            print("Number of neurons: {}".format(sum(self.num_neurons[1:])))
            print("Number of synapses: {}\n".format(self.num_synapses))

        self.is_built = True

    def init_cells(self):
        self._cell_params = {
            'v_thresh': self.config.getfloat('cell', 'v_thresh'),
            'v_reset': self.config.getfloat('cell', 'v_reset'),
            'tau_m': self.config.getfloat('cell', 'tau_m') * self.sim.ms}

    def get_spiketrains(self, **kwargs):
        j = self._spiketrains_container_counter
        if self.spiketrains_n_b_l_t is None or \
                j >= len(self.spiketrains_n_b_l_t):
            return None

        shape = self.spiketrains_n_b_l_t[j][0].shape

        # Outer for-loop that calls this function starts with
        # 'monitor_index' = 0, but this is reserved for the input and handled by
        # `get_spiketrains_input()`.
        i = len(self.spikemonitors) - 1 if kwargs[str('monitor_index')] == -1 \
            else kwargs[str('monitor_index')] + 1
        spiketrain_dict = self.spikemonitors[i].spike_trains()
        spiketrains_flat = np.array([spiketrain_dict[key] / self.sim.ms for key
                                     in spiketrain_dict.keys()])
        spiketrains_b_l_t = self.reshape_flattened_spiketrains(spiketrains_flat,
                                                               shape)
        return spiketrains_b_l_t

    def get_spiketrains_input(self):
        shape = list(self.parsed_model.input_shape) + [self._num_timesteps]
        spiketrain_dict = self.spikemonitors[0].spike_trains()
        spiketrains_flat = np.array([spiketrain_dict[key] / self.sim.ms for key
                                     in spiketrain_dict.keys()])
        spiketrains_b_l_t = self.reshape_flattened_spiketrains(spiketrains_flat,
                                                               shape)
        return spiketrains_b_l_t

    def get_spiketrains_output(self):
        shape = [self.batch_size, self.num_classes, self._num_timesteps]
        spiketrain_dict = self.output_spikemonitor.spike_trains()
        spiketrains_flat = np.array([spiketrain_dict[key] / self.sim.ms for key
                                     in spiketrain_dict.keys()])
        spiketrains_b_l_t = self.reshape_flattened_spiketrains(spiketrains_flat,
                                                               shape)
        return spiketrains_b_l_t

    def get_vmem(self, **kwargs):
        j = kwargs[str('monitor_index')]
        if j >= len(self.statemonitors):
            return None
        try:
            return np.array([
                np.array(v).transpose() for v in self.statemonitors[j].v])
        except AttributeError:
            return None

    def set_biases(self):
        """Set biases.

        Notes
        -----

        This has not been tested yet.
        """

        if any(self._biases):  # TODO: Implement biases.
            warnings.warn("Biases not implemented.", RuntimeWarning)

    def set_spiketrain_stats_input(self):
        AbstractSNN.set_spiketrain_stats_input(self)
