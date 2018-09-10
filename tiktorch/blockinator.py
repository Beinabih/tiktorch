import torch
import numpy as np
import torch.nn.functional as thf
from torch.nn import ReflectionPad2d
# from tiktorch.utils import DynamicShape
from utils import DynamicShape
from contextlib import contextmanager
from device_handler import ModelHandler


class slicey(object):
    def __init__(self, start=None, stop=None, step=None, padding=(0, 0), shape=None):
        if shape is None:
            # Vanilla behaviour
            self.start = self.__istart = start
            self.stop = self.__istop = stop
            self.step = self.__istep = step
            self.padding = self.__ipadding = padding
            self.shape = self.__ishape = shape
        else:
            self.__istart = start
            self.__istop = stop
            self.__istep = step
            self.__ipadding = padding
            self.__ishape = shape
            # Compute real starts and stops
            start = 0 if start is None else start
            stop = shape if stop is None else stop
            # Add in padding
            start -= padding[0]
            stop += padding[1]
            padding = [0, 0]
            # Check if we ran out of volume bounds
            if start < 0:
                padding[0] = -start
                start = 0
            if stop > shape:
                padding[1] = stop - shape
                stop = shape
            # Set
            self.start = start
            self.stop = stop
            self.step = step
            self.padding = tuple(padding)
            self.shape = shape

    @classmethod
    def from_(cls, sl, padding=None, shape=None):
        if isinstance(sl, slice):
            return cls(sl.start, sl.stop, sl.step,
                       padding=((0, 0) if padding is None else padding),
                       shape=shape)
        elif isinstance(sl, slicey):
            return sl
        else:
            raise TypeError

    @property
    def slice(self):
        # noinspection PyTypeChecker
        return slice(self.start, self.stop, self.step)

    @property
    def islice(self):
        return slice(self.__istart, self.__istop, self.__istep)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.start}:{self.stop}/{self.shape}:{self.step} + " \
               f"{self.padding})"


class Blockinator(object):
    def __init__(self, data, dynamic_shape, num_channel_axes=0,
                 pad_fn=(lambda tensor, padding: tensor)):
        # Privates
        self._processor = None
        # Publics
        self.data = data
        self.num_channel_axes = num_channel_axes
        self.dynamic_shape = dynamic_shape
        self.pad_fn = pad_fn

    @property
    def block_shape(self):
        return self.dynamic_shape.base_shape

    @property
    def spatial_shape(self):
        return self.data.shape[self.num_channel_axes:]

    @property
    def num_blocks(self):
        return tuple(shape//size for shape, size in zip(self.spatial_shape, self.block_shape))

    def get_slice(self, *block):
        return tuple(slice(_size * _block, _size * (_block + 1))
                     for _block, _size in zip(block, self.block_shape))

    def space_cake(self, *slices):
        # This function slices the data, and adds a halo if requested.
        # Convert all slice to sliceys
        slices = [slicey.from_(sl) for sl in slices]
        # Pad out-of-array values
        # Get unpadded volume
        unpadded_volume = self.data[tuple(slice(0, None) for _ in range(self.num_channel_axes)) +
                                    tuple(sl.slice for sl in slices)]
        padding = [None] * self.num_channel_axes + [sl.padding for sl in slices]
        # padded_volume = self.pad_fn(unpadded_volume, padding)
        if type(unpadded_volume) is np.ndarray:
            padded_volume = np_pad(unpadded_volume, padding)
        else:
            padded_volume = th_pad(unpadded_volume, padding)

        return padded_volume

    def fetch(self, item):
        # Case: item is a slice object (i.e. slice along the first axis)
        if isinstance(item, slice):
            item = (item,) + (slice(0, None),) * (len(self.spatial_shape) - 1)

        if isinstance(item, tuple):
            if all(isinstance(_elem, int) for _elem in item):
                # Case: item a tuple ints
                full_slice = self.get_slice(*item)
            elif all(isinstance(_elem, slice) for _elem in item):
                # Case: item is a tuple of slices
                # Define helper functions
                def _process_starts(start, num_blocks):
                    if start is None:
                        return 0
                    elif start >= 0:
                        return start
                    else:
                        return num_blocks + start

                def _process_stops(stop, num_blocks):
                    if stop is None:
                        return num_blocks - 1
                    elif stop > 0:
                        return stop - 1
                    else:
                        return num_blocks + stop - 1

                # Get the full slice
                starts = [_process_starts(_sl.start, _num_blocks)
                          for _sl, _num_blocks in zip(item, self.num_blocks)]
                stops = [_process_stops(_sl.stop, _num_blocks)
                         for _sl, _num_blocks in zip(item, self.num_blocks)]
                slice_starts = [_sl.start for _sl in self.get_slice(*starts)]
                slice_stops = [_sl.stop for _sl in self.get_slice(*stops)]
                full_slice = [slice(starts, stops)
                              for starts, stops in zip(slice_starts, slice_stops)]
            else:
                raise NotImplementedError
        else:
            raise NotImplementedError
        # Time to throw in the halo. Check if a processor is attached
        if self._processor is not None and hasattr(self._processor, 'halo'):
            halo = self._processor.halo
        else:
            halo = None
        if halo is not None:
            assert len(halo) == len(self.spatial_shape)
            # Compute halo in units of block size
            num_halo_blocks = [int(np.ceil(_halo / _block_shape))
                               for _halo, _block_shape in zip(halo, self.block_shape)]
            spatial_padding = [(_num_halo_blocks * _block_shape,) * 2
                               for _num_halo_blocks, _block_shape in zip(num_halo_blocks,
                                                                         self.block_shape)]              
            sliceys = [slicey.from_(_sl, _padding, _shape)
                       for _sl, _padding, _shape in zip(full_slice, spatial_padding,
                                                        self.spatial_shape)]
                       
            sliced = self.space_cake(*sliceys)
        else:
            sliced = self.space_cake(*full_slice)
        return sliced

    def __getitem__(self, item):
        return self.fetch(item)

    def process(self):
        pass


    @contextmanager
    def attach(self, processor):
        self._processor = processor
        yield
        self._processor = None


def np_pad(x, padding):
    """
    reflection padding on borders for numpy arrays
    """
    return np.pad(x, padding, mode='reflect')


def th_pad(x, padding):
    """
    reflection padding on borders for torch tensors 
    """
    #merge tuples in list
    padding = [i for sub_list in padding for i in sub_list]
    #torch needs 4d for padding, (N, C, H, W)
    for _ in range(2):
        x = torch.unsqueeze(x,0)
    m = ReflectionPad2d(padding)
    padded_volume = m(x)
    for _ in range(2):
        padded_volume = torch.squeeze(padded_volume, 0)
    return padded_volume


def _test_blocky_basic():
    dynamic_shape = DynamicShape('(32 * (nH + 1), 32 * (nW + 1))')
    block = Blockinator(torch.rand(256, 256), dynamic_shape)
    assert block.num_blocks == (8, 8)
    assert block.get_slice(0, 0) == (slice(0, 32, None), slice(0, 32, None))
    assert list(block[:-1].shape) == [224, 256]


def _test_blocky_halo():
    from argparse import Namespace
    dynamic_shape = DynamicShape('(32 * (nH + 1), 32 * (nW + 1))')
    block = Blockinator(torch.rand(256, 256), dynamic_shape)
    # block = Blockinator(np.random.rand(256,256), dynamic_shape)
    processor = Namespace(halo=[4, 4])
    with block.attach(processor):
        out = block[6:8, 0:2]
    print(out.shape)

def _test_blocky_processor():

    import torch.nn as nn
    model = nn.Sequential(nn.Conv2d(3, 10, 3),
                          nn.Conv2d(10, 10, 3),
                          nn.Conv2d(10, 10, 3),
                          nn.Conv2d(10, 3, 3))
    handler = ModelHandler(model=model,
                           device_names='cpu:0',
                           in_channels=3, out_channels=3,
                           dynamic_shape_code='(32 * (nH + 1), 32 * (nW + 1))')

    dynamic_shape = DynamicShape('(32 * (nH + 1), 32 * (nW + 1))')

    input_tensor = torch.rand(1,3,256,256)

    processor = handler

    for x in range(input_tensor.shape[1]):
        block = Blockinator(input_tensor[0,x], dynamic_shape)

        with block.attach(processor):
            out = block.process()

            if x==0 :
                new_input = torch.unsqueeze(out,0)
            else:
                new_input = torch.cat((new_input,torch.unsqueeze(out,0)),0)

    print (new_input.shape)

    output = handler.forward(input_tensor, handler.device_names[0])





if __name__ == '__main__':
    # _test_blocky_basic()
    # _test_blocky_halo()
    _test_blocky_processor()