# -*- coding: utf-8 -*-

"""Tools for working with epoched data."""

# Authors: Alexandre Gramfort <alexandre.gramfort@inria.fr>
#          Matti Hämäläinen <msh@nmr.mgh.harvard.edu>
#          Daniel Strohmeier <daniel.strohmeier@tu-ilmenau.de>
#          Denis Engemann <denis.engemann@gmail.com>
#          Mainak Jas <mainak@neuro.hut.fi>
#          Stefan Appelhoff <stefan.appelhoff@mailbox.org>
#
# License: BSD (3-clause)

from collections import Counter
from copy import deepcopy
import json
import operator
import os.path as op

import numpy as np

from .io.write import (start_file, start_block, end_file, end_block,
                       write_int, write_float, write_float_matrix,
                       write_double_matrix, write_complex_float_matrix,
                       write_complex_double_matrix, write_id, write_string,
                       _get_split_size, _NEXT_FILE_BUFFER, INT32_MAX)
from .io.meas_info import read_meas_info, write_meas_info, _merge_info
from .io.open import fiff_open, _get_next_fname
from .io.tree import dir_tree_find
from .io.tag import read_tag, read_tag_info
from .io.constants import FIFF
from .io.fiff.raw import _get_fname_rep
from .io.pick import (channel_indices_by_type, channel_type,
                      pick_channels, pick_info, _pick_data_channels,
                      _DATA_CH_TYPES_SPLIT, _picks_to_idx)
from .io.proj import setup_proj, ProjMixin, _proj_equal
from .io.base import BaseRaw, TimeMixin
from .bem import _check_origin
from .evoked import EvokedArray, _check_decim
from .baseline import rescale, _log_rescale, _check_baseline
from .channels.channels import (ContainsMixin, UpdateChannelsMixin,
                                SetChannelsMixin, InterpolationMixin)
from .filter import detrend, FilterMixin, _check_fun
from .parallel import parallel_func

from .event import _read_events_fif, make_fixed_length_events
from .fixes import _get_args, rng_uniform
from .viz import (plot_epochs, plot_epochs_psd, plot_epochs_psd_topomap,
                  plot_epochs_image, plot_topo_image_epochs, plot_drop_log)
from .utils import (_check_fname, check_fname, logger, verbose,
                    _time_mask, check_random_state, warn, _pl,
                    sizeof_fmt, SizeMixin, copy_function_doc_to_method_doc,
                    _check_pandas_installed, _check_preload, GetEpochsMixin,
                    _prepare_read_metadata, _prepare_write_metadata,
                    _check_event_id, _gen_events, _check_option,
                    _check_combine, ShiftTimeMixin, _build_data_frame,
                    _check_pandas_index_arguments, _convert_times,
                    _scale_dataframe_data, _check_time_format, object_size,
                    _on_missing, _validate_type, _ensure_events)
from .utils.docs import fill_doc
from .data.html_templates import epochs_template


def _pack_reject_params(epochs):
    reject_params = dict()
    for key in ('reject', 'flat', 'reject_tmin', 'reject_tmax'):
        val = getattr(epochs, key, None)
        if val is not None:
            reject_params[key] = val
    return reject_params


def _save_split(epochs, fname, part_idx, n_parts, fmt):
    """Split epochs.

    Anything new added to this function also needs to be added to
    BaseEpochs.save to account for new file sizes.
    """
    # insert index in filename
    path, base = op.split(fname)
    idx = base.find('.')
    if part_idx > 0:
        fname = op.join(path, '%s-%d.%s' % (base[:idx], part_idx,
                                            base[idx + 1:]))

    next_fname = None
    if part_idx < n_parts - 1:
        next_fname = op.join(path, '%s-%d.%s' % (base[:idx], part_idx + 1,
                                                 base[idx + 1:]))
        next_idx = part_idx + 1
    else:
        next_idx = None

    with start_file(fname) as fid:
        _save_part(fid, epochs, fmt, n_parts, next_fname, next_idx)


def _save_part(fid, epochs, fmt, n_parts, next_fname, next_idx):
    info = epochs.info
    meas_id = info['meas_id']

    start_block(fid, FIFF.FIFFB_MEAS)
    write_id(fid, FIFF.FIFF_BLOCK_ID)
    if info['meas_id'] is not None:
        write_id(fid, FIFF.FIFF_PARENT_BLOCK_ID, info['meas_id'])

    # Write measurement info
    write_meas_info(fid, info)

    # One or more evoked data sets
    start_block(fid, FIFF.FIFFB_PROCESSED_DATA)
    start_block(fid, FIFF.FIFFB_MNE_EPOCHS)

    # write events out after getting data to ensure bad events are dropped
    data = epochs.get_data()

    _check_option('fmt', fmt, ['single', 'double'])

    if np.iscomplexobj(data):
        if fmt == 'single':
            write_function = write_complex_float_matrix
        elif fmt == 'double':
            write_function = write_complex_double_matrix
    else:
        if fmt == 'single':
            write_function = write_float_matrix
        elif fmt == 'double':
            write_function = write_double_matrix

    start_block(fid, FIFF.FIFFB_MNE_EVENTS)
    write_int(fid, FIFF.FIFF_MNE_EVENT_LIST, epochs.events.T)
    write_string(fid, FIFF.FIFF_DESCRIPTION, _event_id_string(epochs.event_id))
    end_block(fid, FIFF.FIFFB_MNE_EVENTS)

    # Metadata
    if epochs.metadata is not None:
        start_block(fid, FIFF.FIFFB_MNE_METADATA)
        metadata = _prepare_write_metadata(epochs.metadata)
        write_string(fid, FIFF.FIFF_DESCRIPTION, metadata)
        end_block(fid, FIFF.FIFFB_MNE_METADATA)

    # First and last sample
    first = int(round(epochs.tmin * info['sfreq']))  # round just to be safe
    last = first + len(epochs.times) - 1
    write_int(fid, FIFF.FIFF_FIRST_SAMPLE, first)
    write_int(fid, FIFF.FIFF_LAST_SAMPLE, last)

    # save baseline
    if epochs.baseline is not None:
        bmin, bmax = epochs.baseline
        write_float(fid, FIFF.FIFF_MNE_BASELINE_MIN, bmin)
        write_float(fid, FIFF.FIFF_MNE_BASELINE_MAX, bmax)

    # The epochs itself
    decal = np.empty(info['nchan'])
    for k in range(info['nchan']):
        decal[k] = 1.0 / (info['chs'][k]['cal'] *
                          info['chs'][k].get('scale', 1.0))

    data *= decal[np.newaxis, :, np.newaxis]

    write_function(fid, FIFF.FIFF_EPOCH, data)

    # undo modifications to data
    data /= decal[np.newaxis, :, np.newaxis]

    write_string(fid, FIFF.FIFF_MNE_EPOCHS_DROP_LOG,
                 json.dumps(epochs.drop_log))

    reject_params = _pack_reject_params(epochs)
    if reject_params:
        write_string(fid, FIFF.FIFF_MNE_EPOCHS_REJECT_FLAT,
                     json.dumps(reject_params))

    write_int(fid, FIFF.FIFF_MNE_EPOCHS_SELECTION,
              epochs.selection)

    # And now write the next file info in case epochs are split on disk
    if next_fname is not None and n_parts > 1:
        start_block(fid, FIFF.FIFFB_REF)
        write_int(fid, FIFF.FIFF_REF_ROLE, FIFF.FIFFV_ROLE_NEXT_FILE)
        write_string(fid, FIFF.FIFF_REF_FILE_NAME, op.basename(next_fname))
        if meas_id is not None:
            write_id(fid, FIFF.FIFF_REF_FILE_ID, meas_id)
        write_int(fid, FIFF.FIFF_REF_FILE_NUM, next_idx)
        end_block(fid, FIFF.FIFFB_REF)

    end_block(fid, FIFF.FIFFB_MNE_EPOCHS)
    end_block(fid, FIFF.FIFFB_PROCESSED_DATA)
    end_block(fid, FIFF.FIFFB_MEAS)
    end_file(fid)


def _event_id_string(event_id):
    return ';'.join([k + ':' + str(v) for k, v in event_id.items()])


def _merge_events(events, event_id, selection):
    """Merge repeated events."""
    event_id = event_id.copy()
    new_events = events.copy()
    event_idxs_to_delete = list()
    unique_events, counts = np.unique(events[:, 0], return_counts=True)
    for ev in unique_events[counts > 1]:

        # indices at which the non-unique events happened
        idxs = (events[:, 0] == ev).nonzero()[0]

        # Figure out new value for events[:, 1]. Set to 0, if mixed vals exist
        unique_priors = np.unique(events[idxs, 1])
        new_prior = unique_priors[0] if len(unique_priors) == 1 else 0

        # If duplicate time samples have same event val, "merge" == "drop"
        # and no new event_id key will be created
        ev_vals = np.unique(events[idxs, 2])
        if len(ev_vals) <= 1:
            new_event_val = ev_vals[0]

        # Else, make a new event_id for the merged event
        else:

            # Find all event_id keys involved in duplicated events. These
            # keys will be merged to become a new entry in "event_id"
            event_id_keys = list(event_id.keys())
            event_id_vals = list(event_id.values())
            new_key_comps = [event_id_keys[event_id_vals.index(value)]
                             for value in ev_vals]

            # Check if we already have an entry for merged keys of duplicate
            # events ... if yes, reuse it
            for key in event_id:
                if set(key.split('/')) == set(new_key_comps):
                    new_event_val = event_id[key]
                    break

            # Else, find an unused value for the new key and make an entry into
            # the event_id dict
            else:
                ev_vals = np.unique(
                    np.concatenate((list(event_id.values()),
                                    events[:, 1:].flatten()),
                                   axis=0))
                if ev_vals[0] > 1:
                    new_event_val = 1
                else:
                    diffs = np.diff(ev_vals)
                    idx = np.where(diffs > 1)[0]
                    idx = -1 if len(idx) == 0 else idx[0]
                    new_event_val = ev_vals[idx] + 1

                new_event_id_key = '/'.join(sorted(new_key_comps))
                event_id[new_event_id_key] = int(new_event_val)

        # Replace duplicate event times with merged event and remember which
        # duplicate indices to delete later
        new_events[idxs[0], 1] = new_prior
        new_events[idxs[0], 2] = new_event_val
        event_idxs_to_delete.extend(idxs[1:])

    # Delete duplicate event idxs
    new_events = np.delete(new_events, event_idxs_to_delete, 0)
    new_selection = np.delete(selection, event_idxs_to_delete, 0)

    return new_events, event_id, new_selection


def _handle_event_repeated(events, event_id, event_repeated, selection,
                           drop_log):
    """Handle repeated events.

    Note that drop_log will be modified inplace
    """
    assert len(events) == len(selection)
    selection = np.asarray(selection)

    unique_events, u_ev_idxs = np.unique(events[:, 0], return_index=True)

    # Return early if no duplicates
    if len(unique_events) == len(events):
        return events, event_id, selection, drop_log

    # Else, we have duplicates. Triage ...
    _check_option('event_repeated', event_repeated, ['error', 'drop', 'merge'])
    drop_log = list(drop_log)
    if event_repeated == 'error':
        raise RuntimeError('Event time samples were not unique. Consider '
                           'setting the `event_repeated` parameter."')

    elif event_repeated == 'drop':
        logger.info('Multiple event values for single event times found. '
                    'Keeping the first occurrence and dropping all others.')
        new_events = events[u_ev_idxs]
        new_selection = selection[u_ev_idxs]
        drop_ev_idxs = np.setdiff1d(selection, new_selection)
        for idx in drop_ev_idxs:
            drop_log[idx] = drop_log[idx] + ('DROP DUPLICATE',)
        selection = new_selection
    elif event_repeated == 'merge':
        logger.info('Multiple event values for single event times found. '
                    'Creating new event value to reflect simultaneous events.')
        new_events, event_id, new_selection = \
            _merge_events(events, event_id, selection)
        drop_ev_idxs = np.setdiff1d(selection, new_selection)
        for idx in drop_ev_idxs:
            drop_log[idx] = drop_log[idx] + ('MERGE DUPLICATE',)
        selection = new_selection
    drop_log = tuple(drop_log)

    # Remove obsolete kv-pairs from event_id after handling
    keys = new_events[:, 1:].flatten()
    event_id = {k: v for k, v in event_id.items() if v in keys}

    return new_events, event_id, selection, drop_log


@fill_doc
class BaseEpochs(ProjMixin, ContainsMixin, UpdateChannelsMixin, ShiftTimeMixin,
                 SetChannelsMixin, InterpolationMixin, FilterMixin,
                 TimeMixin, SizeMixin, GetEpochsMixin):
    """Abstract base class for `~mne.Epochs`-type classes.

    .. warning:: This class provides basic functionality and should never be
                 instantiated directly.

    Parameters
    ----------
    info : dict
        A copy of the `~mne.Info` dictionary from the raw object.
    data : ndarray | None
        If ``None``, data will be read from the Raw object. If ndarray, must be
        of shape (n_epochs, n_channels, n_times).
    %(epochs_events_event_id)s
    %(epochs_tmin_tmax)s
    %(baseline_epochs)s
        Defaults to ``(None, 0)``, i.e. beginning of the the data until
        time point zero.
    %(epochs_raw)s
    %(picks_all)s
    %(reject_epochs)s
    %(flat)s
    %(decim)s
    %(epochs_reject_tmin_tmax)s
    %(epochs_detrend)s
    %(proj_epochs)s
    %(epochs_on_missing)s
    preload_at_end : bool
        %(epochs_preload)s
    selection : iterable | None
        Iterable of indices of selected epochs. If ``None``, will be
        automatically generated, corresponding to all non-zero events.
    drop_log : tuple | None
        Tuple of tuple of strings indicating which epochs have been marked to
        be ignored.
    filename : str | None
        The filename (if the epochs are read from disk).
    %(epochs_metadata)s
    %(epochs_event_repeated)s
    %(verbose)s

    Notes
    -----
    The ``BaseEpochs`` class is public to allow for stable type-checking in
    user code (i.e., ``isinstance(my_epochs, BaseEpochs)``) but should not be
    used as a constructor for Epochs objects (use instead :class:`mne.Epochs`).
    """

    @verbose
    def __init__(self, info, data, events, event_id=None, tmin=-0.2, tmax=0.5,
                 baseline=(None, 0), raw=None, picks=None, reject=None,
                 flat=None, decim=1, reject_tmin=None, reject_tmax=None,
                 detrend=None, proj=True, on_missing='raise',
                 preload_at_end=False, selection=None, drop_log=None,
                 filename=None, metadata=None, event_repeated='error',
                 verbose=None):  # noqa: D102
        self.verbose = verbose

        if events is not None:  # RtEpochs can have events=None
            events = _ensure_events(events)
            events_max = events.max()
            if events_max > INT32_MAX:
                raise ValueError(
                    f'events array values must not exceed {INT32_MAX}, '
                    f'got {events_max}')
        event_id = _check_event_id(event_id, events)
        self.event_id = event_id
        del event_id

        if events is not None:  # RtEpochs can have events=None
            for key, val in self.event_id.items():
                if val not in events[:, 2]:
                    msg = ('No matching events found for %s '
                           '(event id %i)' % (key, val))
                    _on_missing(on_missing, msg)

            # ensure metadata matches original events size
            self.selection = np.arange(len(events))
            self.events = events
            self.metadata = metadata
            del events

            values = list(self.event_id.values())
            selected = np.where(np.in1d(self.events[:, 2], values))[0]
            if selection is None:
                selection = selected
            else:
                selection = np.array(selection, int)
            if selection.shape != (len(selected),):
                raise ValueError('selection must be shape %s got shape %s'
                                 % (selected.shape, selection.shape))
            self.selection = selection
            if drop_log is None:
                self.drop_log = tuple(
                    () if k in self.selection else ('IGNORED',)
                    for k in range(max(len(self.events),
                                   max(self.selection) + 1)))
            else:
                self.drop_log = drop_log

            self.events = self.events[selected]

            self.events, self.event_id, self.selection, self.drop_log = \
                _handle_event_repeated(
                    self.events, self.event_id, event_repeated,
                    self.selection, self.drop_log)

            # then subselect
            sub = np.where(np.in1d(selection, self.selection))[0]
            if isinstance(metadata, list):
                metadata = [metadata[s] for s in sub]
            elif metadata is not None:
                metadata = metadata.iloc[sub]
            self.metadata = metadata
            del metadata

            n_events = len(self.events)
            if n_events > 1:
                if np.diff(self.events.astype(np.int64)[:, 0]).min() <= 0:
                    warn('The events passed to the Epochs constructor are not '
                         'chronologically ordered.', RuntimeWarning)

            if n_events > 0:
                logger.info('%d matching events found' % n_events)
            else:
                raise ValueError('No desired events found.')
        else:
            self.drop_log = tuple()
            self.selection = np.array([], int)
            self.metadata = metadata
            # do not set self.events here, let subclass do it

        if (detrend not in [None, 0, 1]) or isinstance(detrend, bool):
            raise ValueError('detrend must be None, 0, or 1')
        self.detrend = detrend

        self._raw = raw
        info._check_consistency()
        self.picks = _picks_to_idx(info, picks, none='all', exclude=(),
                                   allow_empty=False)
        self.info = pick_info(info, self.picks)
        del info
        self._current = 0

        if data is None:
            self.preload = False
            self._data = None
            self._do_baseline = True
        else:
            assert decim == 1
            if data.ndim != 3 or data.shape[2] != \
                    round((tmax - tmin) * self.info['sfreq']) + 1:
                raise RuntimeError('bad data shape')
            if data.shape[0] != len(self.events):
                raise ValueError(
                    'The number of epochs and the number of events must match')
            self.preload = True
            self._data = data
            self._do_baseline = False
        self._offset = None

        if tmin > tmax:
            raise ValueError('tmin has to be less than or equal to tmax')

        # Handle times
        sfreq = float(self.info['sfreq'])
        start_idx = int(round(tmin * sfreq))
        self._raw_times = np.arange(start_idx,
                                    int(round(tmax * sfreq)) + 1) / sfreq
        self._set_times(self._raw_times)

        # check reject_tmin and reject_tmax
        if reject_tmin is not None:
            if (np.isclose(reject_tmin, tmin)):
                # adjust for potential small deviations due to sampling freq
                reject_tmin = self.tmin
            elif reject_tmin < tmin:
                raise ValueError(f'reject_tmin needs to be None or >= tmin '
                                 f'(got {reject_tmin})')

        if reject_tmax is not None:
            if (np.isclose(reject_tmax, tmax)):
                # adjust for potential small deviations due to sampling freq
                reject_tmax = self.tmax
            elif reject_tmax > tmax:
                raise ValueError(f'reject_tmax needs to be None or <= tmax '
                                 f'(got {reject_tmax})')

        if (reject_tmin is not None) and (reject_tmax is not None):
            if reject_tmin >= reject_tmax:
                raise ValueError(f'reject_tmin ({reject_tmin}) needs to be '
                                 f' < reject_tmax ({reject_tmax})')

        self.reject_tmin = reject_tmin
        self.reject_tmax = reject_tmax

        # decimation
        self._decim = 1
        self.decimate(decim)

        # baseline correction: replace `None` tuple elements  with actual times
        self.baseline = _check_baseline(baseline, times=self.times,
                                        sfreq=self.info['sfreq'])
        if self.baseline is not None and self.baseline != baseline:
            logger.info(f'Setting baseline interval to '
                        f'[{self.baseline[0]}, {self.baseline[1]}] sec')

        logger.info(_log_rescale(self.baseline))

        # setup epoch rejection
        self.reject = None
        self.flat = None
        self._reject_setup(reject, flat)

        # do the rest
        valid_proj = [True, 'delayed', False]
        if proj not in valid_proj:
            raise ValueError('"proj" must be one of %s, not %s'
                             % (valid_proj, proj))
        if proj == 'delayed':
            self._do_delayed_proj = True
            logger.info('Entering delayed SSP mode.')
        else:
            self._do_delayed_proj = False
        activate = False if self._do_delayed_proj else proj
        self._projector, self.info = setup_proj(self.info, False,
                                                activate=activate)
        if preload_at_end:
            assert self._data is None
            assert self.preload is False
            self.load_data()  # this will do the projection
        elif proj is True and self._projector is not None and data is not None:
            # let's make sure we project if data was provided and proj
            # requested
            # we could do this with np.einsum, but iteration should be
            # more memory safe in most instances
            for ii, epoch in enumerate(self._data):
                self._data[ii] = np.dot(self._projector, epoch)
        self._filename = str(filename) if filename is not None else filename
        self._check_consistency()

    def _check_consistency(self):
        """Check invariants of epochs object."""
        if hasattr(self, 'events'):
            assert len(self.selection) == len(self.events)
            assert len(self.drop_log) >= len(self.events)
        assert len(self.selection) == sum(
            (len(dl) == 0 for dl in self.drop_log))
        assert hasattr(self, '_times_readonly')
        assert not self.times.flags['WRITEABLE']
        assert isinstance(self.drop_log, tuple)
        assert all(isinstance(log, tuple) for log in self.drop_log)
        assert all(isinstance(s, str) for log in self.drop_log for s in log)

    def reset_drop_log_selection(self):
        """Reset the drop_log and selection entries.

        This method will simplify ``self.drop_log`` and ``self.selection``
        so that they are meaningless (tuple of empty tuples and increasing
        integers, respectively). This can be useful when concatenating
        many Epochs instances, as ``drop_log`` can accumulate many entries
        which can become problematic when saving.
        """
        self.selection = np.arange(len(self.events))
        self.drop_log = (tuple(),) * len(self.events)
        self._check_consistency()

    def load_data(self):
        """Load the data if not already preloaded.

        Returns
        -------
        epochs : instance of Epochs
            The epochs object.

        Notes
        -----
        This function operates in-place.

        .. versionadded:: 0.10.0
        """
        if self.preload:
            return self
        self._data = self._get_data()
        self.preload = True
        self._do_baseline = False
        self._decim_slice = slice(None, None, None)
        self._decim = 1
        self._raw_times = self.times
        assert self._data.shape[-1] == len(self.times)
        self._raw = None  # shouldn't need it anymore
        return self

    @verbose
    def decimate(self, decim, offset=0, verbose=None):
        """Decimate the epochs.

        Parameters
        ----------
        %(decim)s
        %(decim_offset)s
        %(verbose_meth)s

        Returns
        -------
        epochs : instance of Epochs
            The decimated Epochs object.

        See Also
        --------
        mne.Evoked.decimate
        mne.Epochs.resample
        mne.io.Raw.resample

        Notes
        -----
        %(decim_notes)s

        If ``decim`` is 1, this method does not copy the underlying data.

        .. versionadded:: 0.10.0

        References
        ----------
        .. footbibliography::
        """
        decim, offset, new_sfreq = _check_decim(self.info, decim, offset)
        start_idx = int(round(-self._raw_times[0] * (self.info['sfreq'] *
                                                     self._decim)))
        self._decim *= decim
        i_start = start_idx % self._decim + offset
        decim_slice = slice(i_start, None, self._decim)
        self.info['sfreq'] = new_sfreq
        if self.preload:
            if decim != 1:
                self._data = self._data[:, :, decim_slice].copy()
                self._raw_times = self._raw_times[decim_slice].copy()
            else:
                self._data = np.ascontiguousarray(self._data)
            self._decim_slice = slice(None)
            self._decim = 1
        else:
            self._decim_slice = decim_slice
        self._set_times(self._raw_times[self._decim_slice])
        return self

    @verbose
    def apply_baseline(self, baseline=(None, 0), *, verbose=None):
        """Baseline correct epochs.

        Parameters
        ----------
        %(baseline_epochs)s
            Defaults to ``(None, 0)``, i.e. beginning of the the data until
            time point zero.
        %(verbose_meth)s

        Returns
        -------
        epochs : instance of Epochs
            The baseline-corrected Epochs object.

        Notes
        -----
        Baseline correction can be done multiple times, but can never be
        reverted once the data has been loaded.

        .. versionadded:: 0.10.0
        """
        baseline = _check_baseline(baseline, times=self.times,
                                   sfreq=self.info['sfreq'])

        if self.preload:
            if self.baseline is not None and baseline is None:
                raise RuntimeError('You cannot remove baseline correction '
                                   'from preloaded data once it has been '
                                   'applied.')
            self._do_baseline = True
            picks = self._detrend_picks
            rescale(self._data, self.times, baseline, copy=False, picks=picks)
            self._do_baseline = False
        else:  # logging happens in "rescale" in "if" branch
            logger.info(_log_rescale(baseline))
            assert self._do_baseline is True
        self.baseline = baseline
        return self

    def _reject_setup(self, reject, flat):
        """Set self._reject_time and self._channel_type_idx."""
        idx = channel_indices_by_type(self.info)
        reject = deepcopy(reject) if reject is not None else dict()
        flat = deepcopy(flat) if flat is not None else dict()
        for rej, kind in zip((reject, flat), ('reject', 'flat')):
            if not isinstance(rej, dict):
                raise TypeError('reject and flat must be dict or None, not %s'
                                % type(rej))
            bads = set(rej.keys()) - set(idx.keys())
            if len(bads) > 0:
                raise KeyError('Unknown channel types found in %s: %s'
                               % (kind, bads))

        for key in idx.keys():
            # don't throw an error if rejection/flat would do nothing
            if len(idx[key]) == 0 and (np.isfinite(reject.get(key, np.inf)) or
                                       flat.get(key, -1) >= 0):
                # This is where we could eventually add e.g.
                # self.allow_missing_reject_keys check to allow users to
                # provide keys that don't exist in data
                raise ValueError("No %s channel found. Cannot reject based on "
                                 "%s." % (key.upper(), key.upper()))

        # check for invalid values
        for rej, kind in zip((reject, flat), ('Rejection', 'Flat')):
            for key, val in rej.items():
                if val is None or val < 0:
                    raise ValueError('%s value must be a number >= 0, not "%s"'
                                     % (kind, val))

        # now check to see if our rejection and flat are getting more
        # restrictive
        old_reject = self.reject if self.reject is not None else dict()
        old_flat = self.flat if self.flat is not None else dict()
        bad_msg = ('{kind}["{key}"] == {new} {op} {old} (old value), new '
                   '{kind} values must be at least as stringent as '
                   'previous ones')
        for key in set(reject.keys()).union(old_reject.keys()):
            old = old_reject.get(key, np.inf)
            new = reject.get(key, np.inf)
            if new > old:
                raise ValueError(bad_msg.format(kind='reject', key=key,
                                                new=new, old=old, op='>'))
        for key in set(flat.keys()).union(old_flat.keys()):
            old = old_flat.get(key, -np.inf)
            new = flat.get(key, -np.inf)
            if new < old:
                raise ValueError(bad_msg.format(kind='flat', key=key,
                                                new=new, old=old, op='<'))

        # after validation, set parameters
        self._bad_dropped = False
        self._channel_type_idx = idx
        self.reject = reject if len(reject) > 0 else None
        self.flat = flat if len(flat) > 0 else None

        if (self.reject_tmin is None) and (self.reject_tmax is None):
            self._reject_time = None
        else:
            if self.reject_tmin is None:
                reject_imin = None
            else:
                idxs = np.nonzero(self.times >= self.reject_tmin)[0]
                reject_imin = idxs[0]
            if self.reject_tmax is None:
                reject_imax = None
            else:
                idxs = np.nonzero(self.times <= self.reject_tmax)[0]
                reject_imax = idxs[-1]
            self._reject_time = slice(reject_imin, reject_imax)

    @verbose  # verbose is used by mne-realtime
    def _is_good_epoch(self, data, verbose=None):
        """Determine if epoch is good."""
        if isinstance(data, str):
            return False, (data,)
        if data is None:
            return False, ('NO_DATA',)
        n_times = len(self.times)
        if data.shape[1] < n_times:
            # epoch is too short ie at the end of the data
            return False, ('TOO_SHORT',)
        if self.reject is None and self.flat is None:
            return True, None
        else:
            if self._reject_time is not None:
                data = data[:, self._reject_time]

            return _is_good(data, self.ch_names, self._channel_type_idx,
                            self.reject, self.flat, full_report=True,
                            ignore_chs=self.info['bads'])

    @verbose
    def _detrend_offset_decim(self, epoch, picks, verbose=None):
        """Aux Function: detrend, baseline correct, offset, decim.

        Note: operates inplace
        """
        if (epoch is None) or isinstance(epoch, str):
            return epoch

        # Detrend
        if self.detrend is not None:
            # We explicitly detrend just data channels (not EMG, ECG, EOG which
            # are processed by baseline correction)
            use_picks = _pick_data_channels(self.info, exclude=())
            epoch[use_picks] = detrend(epoch[use_picks], self.detrend, axis=1)

        # Baseline correct
        if self._do_baseline:
            rescale(
                epoch, self._raw_times, self.baseline, picks=picks, copy=False,
                verbose=False)

        # Decimate if necessary (i.e., epoch not preloaded)
        epoch = epoch[:, self._decim_slice]

        # handle offset
        if self._offset is not None:
            epoch += self._offset

        return epoch

    def iter_evoked(self, copy=False):
        """Iterate over epochs as a sequence of Evoked objects.

        The Evoked objects yielded will each contain a single epoch (i.e., no
        averaging is performed).

        This method resets the object iteration state to the first epoch.

        Parameters
        ----------
        copy : bool
            If False copies of data and measurement info will be omitted
            to save time.
        """
        self.__iter__()

        while True:
            try:
                out = self.__next__(True)
            except StopIteration:
                break
            data, event_id = out
            tmin = self.times[0]
            info = self.info
            if copy:
                info = deepcopy(self.info)
                data = data.copy()

            yield EvokedArray(data, info, tmin, comment=str(event_id))

    def subtract_evoked(self, evoked=None):
        """Subtract an evoked response from each epoch.

        Can be used to exclude the evoked response when analyzing induced
        activity, see e.g. [1]_.

        Parameters
        ----------
        evoked : instance of Evoked | None
            The evoked response to subtract. If None, the evoked response
            is computed from Epochs itself.

        Returns
        -------
        self : instance of Epochs
            The modified instance (instance is also modified inplace).

        References
        ----------
        .. [1] David et al. "Mechanisms of evoked and induced responses in
               MEG/EEG", NeuroImage, vol. 31, no. 4, pp. 1580-1591, July 2006.
        """
        logger.info('Subtracting Evoked from Epochs')
        if evoked is None:
            picks = _pick_data_channels(self.info, exclude=[])
            evoked = self.average(picks)

        # find the indices of the channels to use
        picks = pick_channels(evoked.ch_names, include=self.ch_names)

        # make sure the omitted channels are not data channels
        if len(picks) < len(self.ch_names):
            sel_ch = [evoked.ch_names[ii] for ii in picks]
            diff_ch = list(set(self.ch_names).difference(sel_ch))
            diff_idx = [self.ch_names.index(ch) for ch in diff_ch]
            diff_types = [channel_type(self.info, idx) for idx in diff_idx]
            bad_idx = [diff_types.index(t) for t in diff_types if t in
                       _DATA_CH_TYPES_SPLIT]
            if len(bad_idx) > 0:
                bad_str = ', '.join([diff_ch[ii] for ii in bad_idx])
                raise ValueError('The following data channels are missing '
                                 'in the evoked response: %s' % bad_str)
            logger.info('    The following channels are not included in the '
                        'subtraction: %s' % ', '.join(diff_ch))

        # make sure the times match
        if (len(self.times) != len(evoked.times) or
                np.max(np.abs(self.times - evoked.times)) >= 1e-7):
            raise ValueError('Epochs and Evoked object do not contain '
                             'the same time points.')

        # handle SSPs
        if not self.proj and evoked.proj:
            warn('Evoked has SSP applied while Epochs has not.')
        if self.proj and not evoked.proj:
            evoked = evoked.copy().apply_proj()

        # find the indices of the channels to use in Epochs
        ep_picks = [self.ch_names.index(evoked.ch_names[ii]) for ii in picks]

        # do the subtraction
        if self.preload:
            self._data[:, ep_picks, :] -= evoked.data[picks][None, :, :]
        else:
            if self._offset is None:
                self._offset = np.zeros((len(self.ch_names), len(self.times)),
                                        dtype=np.float64)
            self._offset[ep_picks] -= evoked.data[picks]
        logger.info('[done]')

        return self

    @fill_doc
    def average(self, picks=None, method="mean"):
        """Compute an average over epochs.

        Parameters
        ----------
        %(picks_all_data)s
        method : str | callable
            How to combine the data. If "mean"/"median", the mean/median
            are returned.
            Otherwise, must be a callable which, when passed an array of shape
            (n_epochs, n_channels, n_time) returns an array of shape
            (n_channels, n_time).
            Note that due to file type limitations, the kind for all
            these will be "average".

        Returns
        -------
        evoked : instance of Evoked | dict of Evoked
            The averaged epochs.

        Notes
        -----
        Computes an average of all epochs in the instance, even if
        they correspond to different conditions. To average by condition,
        do ``epochs[condition].average()`` for each condition separately.

        When picks is None and epochs contain only ICA channels, no channels
        are selected, resulting in an error. This is because ICA channels
        are not considered data channels (they are of misc type) and only data
        channels are selected when picks is None.

        The ``method`` parameter allows e.g. robust averaging.
        For example, one could do:

            >>> from scipy.stats import trim_mean  # doctest:+SKIP
            >>> trim = lambda x: trim_mean(x, 0.1, axis=0)  # doctest:+SKIP
            >>> epochs.average(method=trim)  # doctest:+SKIP

        This would compute the trimmed mean.
        """
        return self._compute_aggregate(picks=picks, mode=method)

    @fill_doc
    def standard_error(self, picks=None):
        """Compute standard error over epochs.

        Parameters
        ----------
        %(picks_all_data)s

        Returns
        -------
        evoked : instance of Evoked
            The standard error over epochs.
        """
        return self._compute_aggregate(picks, "std")

    def _compute_aggregate(self, picks, mode='mean'):
        """Compute the mean, median, or std over epochs and return Evoked."""
        # if instance contains ICA channels they won't be included unless picks
        # is specified
        if picks is None:
            check_ICA = [x.startswith('ICA') for x in self.ch_names]
            if np.all(check_ICA):
                raise TypeError('picks must be specified (i.e. not None) for '
                                'ICA channel data')
            elif np.any(check_ICA):
                warn('ICA channels will not be included unless explicitly '
                     'selected in picks')

        n_channels = len(self.ch_names)
        n_times = len(self.times)

        if self.preload:
            n_events = len(self.events)
            fun = _check_combine(mode, valid=('mean', 'median', 'std'))
            data = fun(self._data)
            assert len(self.events) == len(self._data)
            if data.shape != self._data.shape[1:]:
                raise RuntimeError(
                    'You passed a function that resulted n data of shape {}, '
                    'but it should be {}.'.format(
                        data.shape, self._data.shape[1:]))
        else:
            if mode not in {"mean", "std"}:
                raise ValueError("If data are not preloaded, can only compute "
                                 "mean or standard deviation.")
            data = np.zeros((n_channels, n_times))
            n_events = 0
            for e in self:
                if np.iscomplexobj(e):
                    data = data.astype(np.complex128)
                data += e
                n_events += 1

            if n_events > 0:
                data /= n_events
            else:
                data.fill(np.nan)

            # convert to stderr if requested, could do in one pass but do in
            # two (slower) in case there are large numbers
            if mode == "std":
                data_mean = data.copy()
                data.fill(0.)
                for e in self:
                    data += (e - data_mean) ** 2
                data = np.sqrt(data / n_events)

        if mode == "std":
            kind = 'standard_error'
            data /= np.sqrt(n_events)
        else:
            kind = "average"

        return self._evoked_from_epoch_data(data, self.info, picks, n_events,
                                            kind, self._name)

    @property
    def _name(self):
        """Give a nice string representation based on event ids."""
        if len(self.event_id) == 1:
            comment = next(iter(self.event_id.keys()))
        else:
            count = Counter(self.events[:, 2])
            comments = list()
            for key, value in self.event_id.items():
                comments.append('%.2f × %s' % (
                    float(count[value]) / len(self.events), key))
            comment = ' + '.join(comments)
        return comment

    def _evoked_from_epoch_data(self, data, info, picks, n_events, kind,
                                comment):
        """Create an evoked object from epoch data."""
        info = deepcopy(info)
        # don't apply baseline correction; we'll set evoked.baseline manually
        evoked = EvokedArray(data, info, tmin=self.times[0], comment=comment,
                             nave=n_events, kind=kind, baseline=None,
                             verbose=self.verbose)
        evoked.baseline = self.baseline

        # XXX: above constructor doesn't recreate the times object precisely
        evoked.times = self.times.copy()

        # pick channels
        picks = _picks_to_idx(self.info, picks, 'data_or_ica', ())
        ch_names = [evoked.ch_names[p] for p in picks]
        evoked.pick_channels(ch_names)

        if len(evoked.info['ch_names']) == 0:
            raise ValueError('No data channel found when averaging.')

        if evoked.nave < 1:
            warn('evoked object is empty (based on less than 1 epoch)')

        return evoked

    @property
    def ch_names(self):
        """Channel names."""
        return self.info['ch_names']

    @copy_function_doc_to_method_doc(plot_epochs)
    def plot(self, picks=None, scalings=None, n_epochs=20, n_channels=20,
             title=None, events=None, event_color=None,
             order=None, show=True, block=False, decim='auto', noise_cov=None,
             butterfly=False, show_scrollbars=True, epoch_colors=None,
             event_id=None, group_by='type'):
        return plot_epochs(self, picks=picks, scalings=scalings,
                           n_epochs=n_epochs, n_channels=n_channels,
                           title=title, events=events, event_color=event_color,
                           order=order, show=show, block=block, decim=decim,
                           noise_cov=noise_cov, butterfly=butterfly,
                           show_scrollbars=show_scrollbars,
                           epoch_colors=epoch_colors, event_id=event_id,
                           group_by=group_by)

    @copy_function_doc_to_method_doc(plot_epochs_psd)
    def plot_psd(self, fmin=0, fmax=np.inf, tmin=None, tmax=None,
                 proj=False, bandwidth=None, adaptive=False, low_bias=True,
                 normalization='length', picks=None, ax=None, color='black',
                 xscale='linear', area_mode='std', area_alpha=0.33,
                 dB=True, estimate='auto', show=True, n_jobs=1,
                 average=False, line_alpha=None, spatial_colors=True,
                 sphere=None, verbose=None):
        return plot_epochs_psd(self, fmin=fmin, fmax=fmax, tmin=tmin,
                               tmax=tmax, proj=proj, bandwidth=bandwidth,
                               adaptive=adaptive, low_bias=low_bias,
                               normalization=normalization, picks=picks, ax=ax,
                               color=color, xscale=xscale, area_mode=area_mode,
                               area_alpha=area_alpha, dB=dB, estimate=estimate,
                               show=show, n_jobs=n_jobs, average=average,
                               line_alpha=line_alpha,
                               spatial_colors=spatial_colors, sphere=sphere,
                               verbose=verbose)

    @copy_function_doc_to_method_doc(plot_epochs_psd_topomap)
    def plot_psd_topomap(self, bands=None, tmin=None,
                         tmax=None, proj=False, bandwidth=None, adaptive=False,
                         low_bias=True, normalization='length', ch_type=None,
                         cmap=None, agg_fun=None, dB=True,
                         n_jobs=1, normalize=False, cbar_fmt='auto',
                         outlines='head', axes=None, show=True,
                         sphere=None, vlim=(None, None), verbose=None):
        return plot_epochs_psd_topomap(
            self, bands=bands, tmin=tmin, tmax=tmax,
            proj=proj, bandwidth=bandwidth, adaptive=adaptive,
            low_bias=low_bias, normalization=normalization, ch_type=ch_type,
            cmap=cmap, agg_fun=agg_fun, dB=dB, n_jobs=n_jobs,
            normalize=normalize, cbar_fmt=cbar_fmt, outlines=outlines,
            axes=axes, show=show, sphere=sphere, vlim=vlim, verbose=verbose)

    @copy_function_doc_to_method_doc(plot_topo_image_epochs)
    def plot_topo_image(self, layout=None, sigma=0., vmin=None, vmax=None,
                        colorbar=None, order=None, cmap='RdBu_r',
                        layout_scale=.95, title=None, scalings=None,
                        border='none', fig_facecolor='k', fig_background=None,
                        font_color='w', show=True):
        return plot_topo_image_epochs(
            self, layout=layout, sigma=sigma, vmin=vmin, vmax=vmax,
            colorbar=colorbar, order=order, cmap=cmap,
            layout_scale=layout_scale, title=title, scalings=scalings,
            border=border, fig_facecolor=fig_facecolor,
            fig_background=fig_background, font_color=font_color, show=show)

    @verbose
    def drop_bad(self, reject='existing', flat='existing', verbose=None):
        """Drop bad epochs without retaining the epochs data.

        Should be used before slicing operations.

        .. warning:: This operation is slow since all epochs have to be read
                     from disk. To avoid reading epochs from disk multiple
                     times, use :meth:`mne.Epochs.load_data()`.

        Parameters
        ----------
        %(reject_drop_bad)s
        %(flat_drop_bad)s
        %(verbose_meth)s

        Returns
        -------
        epochs : instance of Epochs
            The epochs with bad epochs dropped. Operates in-place.

        Notes
        -----
        Dropping bad epochs can be done multiple times with different
        ``reject`` and ``flat`` parameters. However, once an epoch is
        dropped, it is dropped forever, so if more lenient thresholds may
        subsequently be applied, `epochs.copy <mne.Epochs.copy>` should be
        used.
        """
        if reject == 'existing':
            if flat == 'existing' and self._bad_dropped:
                return
            reject = self.reject
        if flat == 'existing':
            flat = self.flat
        if any(isinstance(rej, str) and rej != 'existing' for
               rej in (reject, flat)):
            raise ValueError('reject and flat, if strings, must be "existing"')
        self._reject_setup(reject, flat)
        self._get_data(out=False, verbose=verbose)
        return self

    def drop_log_stats(self, ignore=('IGNORED',)):
        """Compute the channel stats based on a drop_log from Epochs.

        Parameters
        ----------
        ignore : list
            The drop reasons to ignore.

        Returns
        -------
        perc : float
            Total percentage of epochs dropped.

        See Also
        --------
        plot_drop_log
        """
        return _drop_log_stats(self.drop_log, ignore)

    @copy_function_doc_to_method_doc(plot_drop_log)
    def plot_drop_log(self, threshold=0, n_max_plot=20, subject='Unknown subj',
                      color=(0.9, 0.9, 0.9), width=0.8, ignore=('IGNORED',),
                      show=True):
        if not self._bad_dropped:
            raise ValueError("You cannot use plot_drop_log since bad "
                             "epochs have not yet been dropped. "
                             "Use epochs.drop_bad().")
        return plot_drop_log(self.drop_log, threshold, n_max_plot, subject,
                             color=color, width=width, ignore=ignore,
                             show=show)

    @copy_function_doc_to_method_doc(plot_epochs_image)
    def plot_image(self, picks=None, sigma=0., vmin=None, vmax=None,
                   colorbar=True, order=None, show=True, units=None,
                   scalings=None, cmap=None, fig=None, axes=None,
                   overlay_times=None, combine=None, group_by=None,
                   evoked=True, ts_args=None, title=None, clear=False):
        return plot_epochs_image(self, picks=picks, sigma=sigma, vmin=vmin,
                                 vmax=vmax, colorbar=colorbar, order=order,
                                 show=show, units=units, scalings=scalings,
                                 cmap=cmap, fig=fig, axes=axes,
                                 overlay_times=overlay_times, combine=combine,
                                 group_by=group_by, evoked=evoked,
                                 ts_args=ts_args, title=title, clear=clear)

    @verbose
    def drop(self, indices, reason='USER', verbose=None):
        """Drop epochs based on indices or boolean mask.

        .. note:: The indices refer to the current set of undropped epochs
                  rather than the complete set of dropped and undropped epochs.
                  They are therefore not necessarily consistent with any
                  external indices (e.g., behavioral logs). To drop epochs
                  based on external criteria, do not use the ``preload=True``
                  flag when constructing an Epochs object, and call this
                  method before calling the :meth:`mne.Epochs.drop_bad` or
                  :meth:`mne.Epochs.load_data` methods.

        Parameters
        ----------
        indices : array of int or bool
            Set epochs to remove by specifying indices to remove or a boolean
            mask to apply (where True values get removed). Events are
            correspondingly modified.
        reason : str
            Reason for dropping the epochs ('ECG', 'timeout', 'blink' etc).
            Default: 'USER'.
        %(verbose_meth)s

        Returns
        -------
        epochs : instance of Epochs
            The epochs with indices dropped. Operates in-place.
        """
        indices = np.atleast_1d(indices)

        if indices.ndim > 1:
            raise ValueError("indices must be a scalar or a 1-d array")

        if indices.dtype == bool:
            indices = np.where(indices)[0]
        try_idx = np.where(indices < 0, indices + len(self.events), indices)

        out_of_bounds = (try_idx < 0) | (try_idx >= len(self.events))
        if out_of_bounds.any():
            first = indices[out_of_bounds][0]
            raise IndexError("Epoch index %d is out of bounds" % first)
        keep = np.setdiff1d(np.arange(len(self.events)), try_idx)
        self._getitem(keep, reason, copy=False, drop_event_id=False)
        count = len(try_idx)
        logger.info('Dropped %d epoch%s: %s' %
                    (count, _pl(count), ', '.join(map(str, np.sort(try_idx)))))

        return self

    def _get_epoch_from_raw(self, idx, verbose=None):
        """Get a given epoch from disk."""
        raise NotImplementedError

    def _project_epoch(self, epoch):
        """Process a raw epoch based on the delayed param."""
        # whenever requested, the first epoch is being projected.
        if (epoch is None) or isinstance(epoch, str):
            # can happen if t < 0 or reject based on annotations
            return epoch
        proj = self._do_delayed_proj or self.proj
        if self._projector is not None and proj is True:
            epoch = np.dot(self._projector, epoch)
        return epoch

    @verbose
    def _get_data(self, out=True, picks=None, item=None, verbose=None):
        """Load all data, dropping bad epochs along the way.

        Parameters
        ----------
        out : bool
            Return the data. Setting this to False is used to reject bad
            epochs without caching all the data, which saves memory.
        %(picks_all)s
        %(verbose_meth)s
        """
        if item is None:
            item = slice(None)
        elif not self._bad_dropped:
            raise ValueError(
                'item must be None in epochs.get_data() unless bads have been '
                'dropped. Consider using epochs.drop_bad().')
        select = self._item_to_select(item)  # indices or slice
        use_idx = np.arange(len(self.events))[select]
        n_events = len(use_idx)
        # in case there are no good events
        if self.preload:
            # we will store our result in our existing array
            data = self._data
        else:
            # we start out with an empty array, allocate only if necessary
            data = np.empty((0, len(self.info['ch_names']), len(self.times)))
            logger.info('Loading data for %s events and %s original time '
                        'points ...' % (n_events, len(self._raw_times)))

        if self._bad_dropped:
            if not out:
                return
            if self.preload:
                data = data[select]
                if picks is None:
                    return data
                else:
                    picks = _picks_to_idx(self.info, picks)
                    return data[:, picks]

            # we need to load from disk, drop, and return data
            detrend_picks = self._detrend_picks
            for ii, idx in enumerate(use_idx):
                # faster to pre-allocate memory here
                epoch_noproj = self._get_epoch_from_raw(idx)
                epoch_noproj = self._detrend_offset_decim(
                    epoch_noproj, detrend_picks)
                if self._do_delayed_proj:
                    epoch_out = epoch_noproj
                else:
                    epoch_out = self._project_epoch(epoch_noproj)
                if ii == 0:
                    data = np.empty((n_events, len(self.ch_names),
                                     len(self.times)), dtype=epoch_out.dtype)
                data[ii] = epoch_out
        else:
            # bads need to be dropped, this might occur after a preload
            # e.g., when calling drop_bad w/new params
            good_idx = []
            n_out = 0
            drop_log = list(self.drop_log)
            assert n_events == len(self.selection)
            if not self.preload:
                detrend_picks = self._detrend_picks
            for idx, sel in enumerate(self.selection):
                if self.preload:  # from memory
                    if self._do_delayed_proj:
                        epoch_noproj = self._data[idx]
                        epoch = self._project_epoch(epoch_noproj)
                    else:
                        epoch_noproj = None
                        epoch = self._data[idx]
                else:  # from disk
                    epoch_noproj = self._get_epoch_from_raw(idx)
                    epoch_noproj = self._detrend_offset_decim(
                        epoch_noproj, detrend_picks)
                    epoch = self._project_epoch(epoch_noproj)

                epoch_out = epoch_noproj if self._do_delayed_proj else epoch
                is_good, bad_tuple = self._is_good_epoch(
                    epoch, verbose=verbose)
                if not is_good:
                    assert isinstance(bad_tuple, tuple)
                    assert all(isinstance(x, str) for x in bad_tuple)
                    drop_log[sel] = drop_log[sel] + bad_tuple
                    continue
                good_idx.append(idx)

                # store the epoch if there is a reason to (output or update)
                if out or self.preload:
                    # faster to pre-allocate, then trim as necessary
                    if n_out == 0 and not self.preload:
                        data = np.empty((n_events, epoch_out.shape[0],
                                         epoch_out.shape[1]),
                                        dtype=epoch_out.dtype, order='C')
                    data[n_out] = epoch_out
                    n_out += 1
            self.drop_log = tuple(drop_log)
            del drop_log

            self._bad_dropped = True
            logger.info("%d bad epochs dropped" % (n_events - len(good_idx)))

            # adjust the data size if there is a reason to (output or update)
            if out or self.preload:
                if data.flags['OWNDATA'] and data.flags['C_CONTIGUOUS']:
                    data.resize((n_out,) + data.shape[1:], refcheck=False)
                else:
                    data = data[:n_out]
                    if self.preload:
                        self._data = data

            # Now update our properties (excepd data, which is already fixed)
            self._getitem(good_idx, None, copy=False, drop_event_id=False,
                          select_data=False)

        if out:
            if picks is None:
                return data
            else:
                picks = _picks_to_idx(self.info, picks)
                return data[:, picks]
        else:
            return None

    @property
    def _detrend_picks(self):
        if self._do_baseline:
            return _pick_data_channels(
                self.info, with_ref_meg=True, with_aux=True, exclude=())
        else:
            return []

    @fill_doc
    def get_data(self, picks=None, item=None):
        """Get all epochs as a 3D array.

        Parameters
        ----------
        %(picks_all)s
        item : slice | array-like | str | list | None
            The items to get. See :meth:`mne.Epochs.__getitem__` for
            a description of valid options. This can be substantially faster
            for obtaining an ndarray than :meth:`~mne.Epochs.__getitem__`
            for repeated access on large Epochs objects.
            None (default) is an alias for ``slice(None)``.

            .. versionadded:: 0.20

        Returns
        -------
        data : array of shape (n_epochs, n_channels, n_times)
            A view on epochs data.
        """
        return self._get_data(picks=picks, item=item)

    @verbose
    def apply_function(self, fun, picks=None, dtype=None, n_jobs=1,
                       channel_wise=True, verbose=None, *args, **kwargs):
        """Apply a function to a subset of channels.

        %(applyfun_summary_epochs)s

        Parameters
        ----------
        %(applyfun_fun)s
        %(picks_all_data_noref)s
        %(applyfun_dtype)s
        %(n_jobs)s
        %(applyfun_chwise)s
        %(verbose_meth)s
        %(arg_fun)s
        %(kwarg_fun)s

        Returns
        -------
        self : instance of Epochs
            The epochs object with transformed data.
        """
        _check_preload(self, 'epochs.apply_function')
        picks = _picks_to_idx(self.info, picks, exclude=(), with_ref_meg=False)

        if not callable(fun):
            raise ValueError('fun needs to be a function')

        data_in = self._data
        if dtype is not None and dtype != self._data.dtype:
            self._data = self._data.astype(dtype)

        if channel_wise:
            if n_jobs == 1:
                # modify data inplace to save memory
                for idx in picks:
                    self._data[:, idx, :] = _check_fun(fun, data_in[:, idx, :],
                                                       *args, **kwargs)
            else:
                # use parallel function
                parallel, p_fun, _ = parallel_func(_check_fun, n_jobs)
                data_picks_new = parallel(p_fun(
                    fun, data_in[:, p, :], *args, **kwargs) for p in picks)
                for pp, p in enumerate(picks):
                    self._data[:, p, :] = data_picks_new[pp]
        else:
            self._data = _check_fun(
                fun, data_in, *args, **kwargs)

        return self

    @property
    def times(self):
        """Time vector in seconds."""
        return self._times_readonly

    def _set_times(self, times):
        """Set self._times_readonly (and make it read only)."""
        # naming used to indicate that it shouldn't be
        # changed directly, but rather via this method
        self._times_readonly = times.copy()
        self._times_readonly.flags['WRITEABLE'] = False

    @property
    def tmin(self):
        """First time point."""
        return self.times[0]

    @property
    def filename(self):
        """The filename."""
        return self._filename

    @property
    def tmax(self):
        """Last time point."""
        return self.times[-1]

    def __repr__(self):
        """Build string representation."""
        s = ' %s events ' % len(self.events)
        s += '(all good)' if self._bad_dropped else '(good & bad)'
        s += ', %g - %g sec' % (self.tmin, self.tmax)
        s += ', baseline '
        if self.baseline is None:
            s += 'off'
        else:
            s += f'{self.baseline[0]:g} – {self.baseline[1]:g} sec'
            if self.baseline != _check_baseline(
                    self.baseline, times=self.times, sfreq=self.info['sfreq'],
                    on_baseline_outside_data='adjust'):
                s += ' (baseline period was cropped after baseline correction)'

        s += ', ~%s' % (sizeof_fmt(self._size),)
        s += ', data%s loaded' % ('' if self.preload else ' not')
        s += ', with metadata' if self.metadata is not None else ''
        counts = ['%r: %i' % (k, sum(self.events[:, 2] == v))
                  for k, v in sorted(self.event_id.items())]
        if len(self.event_id) > 0:
            s += ',' + '\n '.join([''] + counts)
        class_name = self.__class__.__name__
        class_name = 'Epochs' if class_name == 'BaseEpochs' else class_name
        return '<%s | %s>' % (class_name, s)

    def _repr_html_(self):
        if self.baseline is None:
            baseline = 'off'
        else:
            baseline = tuple([f'{b:.3f}' for b in self.baseline])
            baseline = f'{baseline[0]} – {baseline[1]} sec'

        if isinstance(self.event_id, dict):
            events = ''
            for k, v in sorted(self.event_id.items()):
                n_events = sum(self.events[:, 2] == v)
                events += f'{k}: {n_events}<br>'
        elif isinstance(self.event_id, list):
            events = ''
            for k in self.event_id:
                n_events = sum(self.events[:, 2] == k)
                events += f'{k}: {n_events}<br>'
        elif isinstance(self.event_id, int):
            n_events = len(self.events[:, 2])
            events = f'{self.event_id}: {n_events}<br>'
        else:
            events = None
        return epochs_template.substitute(epochs=self, baseline=baseline,
                                          events=events)

    @verbose
    def crop(self, tmin=None, tmax=None, include_tmax=True, verbose=None):
        """Crop a time interval from the epochs.

        Parameters
        ----------
        tmin : float | None
            Start time of selection in seconds.
        tmax : float | None
            End time of selection in seconds.
        %(include_tmax)s
        %(verbose_meth)s

        Returns
        -------
        epochs : instance of Epochs
            The cropped epochs object, modified in-place.

        Notes
        -----
        %(notes_tmax_included_by_default)s
        """
        # XXX this could be made to work on non-preloaded data...
        _check_preload(self, 'Modifying data of epochs')

        if tmin is None:
            tmin = self.tmin
        elif tmin < self.tmin:
            warn('tmin is not in epochs time interval. tmin is set to '
                 'epochs.tmin')
            tmin = self.tmin

        if tmax is None:
            tmax = self.tmax
        elif tmax > self.tmax:
            warn('tmax is not in epochs time interval. tmax is set to '
                 'epochs.tmax')
            tmax = self.tmax

        tmask = _time_mask(self.times, tmin, tmax, sfreq=self.info['sfreq'],
                           include_tmax=include_tmax)
        self._set_times(self.times[tmask])
        self._raw_times = self._raw_times[tmask]
        self._data = self._data[:, :, tmask]

        # Adjust rejection period
        if self.reject_tmin is not None and self.reject_tmin < self.tmin:
            logger.info(
                f'reject_tmin is not in epochs time interval. '
                f'Setting reject_tmin to epochs.tmin ({self.tmin} sec)')
            self.reject_tmin = self.tmin
        if self.reject_tmax is not None and self.reject_tmax > self.tmax:
            logger.info(
                f'reject_tmax is not in epochs time interval. '
                f'Setting reject_tmax to epochs.tmax ({self.tmax} sec)')
            self.reject_tmax = self.tmax
        return self

    def copy(self):
        """Return copy of Epochs instance.

        Returns
        -------
        epochs : instance of Epochs
            A copy of the object.
        """
        return deepcopy(self)

    def __deepcopy__(self, memodict):
        """Make a deepcopy."""
        cls = self.__class__
        result = cls.__new__(cls)
        for k, v in self.__dict__.items():
            # drop_log is immutable and _raw is private (and problematic to
            # deepcopy)
            if k in ('drop_log', '_raw', '_times_readonly'):
                memodict[id(v)] = v
            else:
                v = deepcopy(v, memodict)
            result.__dict__[k] = v
        return result

    @verbose
    def save(self, fname, split_size='2GB', fmt='single', overwrite=False,
             verbose=True):
        """Save epochs in a fif file.

        Parameters
        ----------
        fname : str
            The name of the file, which should end with -epo.fif or
            -epo.fif.gz.
        split_size : str | int
            Large raw files are automatically split into multiple pieces. This
            parameter specifies the maximum size of each piece. If the
            parameter is an integer, it specifies the size in Bytes. It is
            also possible to pass a human-readable string, e.g., 100MB.
            Note: Due to FIFF file limitations, the maximum split size is 2GB.

            .. versionadded:: 0.10.0
        fmt : str
            Format to save data. Valid options are 'double' or
            'single' for 64- or 32-bit float, or for 128- or
            64-bit complex numbers respectively. Note: Data are processed with
            double precision. Choosing single-precision, the saved data
            will slightly differ due to the reduction in precision.

            .. versionadded:: 0.17
        %(overwrite)s
            To overwrite original file (the same one that was loaded),
            data must be preloaded upon reading. This defaults to True in 0.18
            but will change to False in 0.19.

            .. versionadded:: 0.18
        %(verbose_meth)s

        Notes
        -----
        Bad epochs will be dropped before saving the epochs to disk.
        """
        check_fname(fname, 'epochs', ('-epo.fif', '-epo.fif.gz',
                                      '_epo.fif', '_epo.fif.gz'))

        # check for file existence
        _check_fname(fname, overwrite)

        split_size_bytes = _get_split_size(split_size)

        _check_option('fmt', fmt, ['single', 'double'])

        # to know the length accurately. The get_data() call would drop
        # bad epochs anyway
        self.drop_bad()
        # total_size tracks sizes that get split
        # over_size tracks overhead (tags, things that get written to each)
        if len(self) == 0:
            warn('Saving epochs with no data')
            total_size = 0
        else:
            d = self[0].get_data()
            # this should be guaranteed by subclasses
            assert d.dtype in ('>f8', '<f8', '>c16', '<c16')
            total_size = d.nbytes * len(self)
        self._check_consistency()
        over_size = 0
        if fmt == "single":
            total_size //= 2  # 64bit data converted to 32bit before writing.
        over_size += 32  # FIF tags
        # Account for all the other things we write, too
        # 1. meas_id block plus main epochs block
        over_size += 132
        # 2. measurement info (likely slight overestimate, but okay)
        over_size += object_size(self.info) + 16 * len(self.info)
        # 3. events and event_id in its own block
        total_size += self.events.size * 4
        over_size += len(_event_id_string(self.event_id)) + 72
        # 4. Metadata in a block of its own
        if self.metadata is not None:
            total_size += len(_prepare_write_metadata(self.metadata))
        over_size += 56
        # 5. first sample, last sample, baseline
        over_size += 40 * (self.baseline is not None) + 40
        # 6. drop log: gets written to each, with IGNORE for ones that are
        #    not part of it. So make a fake one with all having entries.
        drop_size = len(json.dumps(self.drop_log)) + 16
        drop_size += 8 * (len(self.selection) - 1)  # worst case: all but one
        over_size += drop_size
        # 7. reject params
        reject_params = _pack_reject_params(self)
        if reject_params:
            over_size += len(json.dumps(reject_params)) + 16
        # 8. selection
        total_size += self.selection.size * 4
        over_size += 16
        # 9. end of file tags
        over_size += _NEXT_FILE_BUFFER
        logger.debug(f'    Overhead size:   {str(over_size).rjust(15)}')
        logger.debug(f'    Splittable size: {str(total_size).rjust(15)}')
        logger.debug(f'    Split size:      {str(split_size_bytes).rjust(15)}')
        # need at least one per
        n_epochs = len(self)
        n_per = total_size // n_epochs if n_epochs else 0
        min_size = n_per + over_size
        if split_size_bytes < min_size:
            raise ValueError(
                f'The split size {split_size} is too small to safely write '
                'the epochs contents, minimum split size is '
                f'{sizeof_fmt(min_size)} ({min_size} bytes)')

        # This is like max(int(ceil(total_size / split_size)), 1) but cleaner
        n_parts = max(
            (total_size - 1) // (split_size_bytes - over_size) + 1, 1)
        assert n_parts >= 1, n_parts
        if n_parts > 1:
            logger.info(f'Splitting into {n_parts} parts')
            if n_parts > 100:  # This must be an error
                raise ValueError(
                    f'Split size {split_size} would result in writing '
                    f'{n_parts} files')

        if len(self.drop_log) > 100000:
            warn(f'epochs.drop_log contains {len(self.drop_log)} entries '
                 f'which will incur up to a {sizeof_fmt(drop_size)} writing '
                 f'overhead (per split file), consider using '
                 f'epochs.reset_drop_log_selection() prior to writing')

        epoch_idxs = np.array_split(np.arange(n_epochs), n_parts)

        for part_idx, epoch_idx in enumerate(epoch_idxs):
            this_epochs = self[epoch_idx] if n_parts > 1 else self
            # avoid missing event_ids in splits
            this_epochs.event_id = self.event_id
            _save_split(this_epochs, fname, part_idx, n_parts, fmt)

    def equalize_event_counts(self, event_ids, method='mintime'):
        """Equalize the number of trials in each condition.

        It tries to make the remaining epochs occurring as close as possible in
        time. This method works based on the idea that if there happened to be
        some time-varying (like on the scale of minutes) noise characteristics
        during a recording, they could be compensated for (to some extent) in
        the equalization process. This method thus seeks to reduce any of
        those effects by minimizing the differences in the times of the events
        in the two sets of epochs. For example, if one had event times
        [1, 2, 3, 4, 120, 121] and the other one had [3.5, 4.5, 120.5, 121.5],
        it would remove events at times [1, 2] in the first epochs and not
        [120, 121].

        Parameters
        ----------
        event_ids : list
            The event types to equalize. Each entry in the list can either be
            a str (single event) or a list of str. In the case where one of
            the entries is a list of str, event_ids in that list will be
            grouped together before equalizing trial counts across conditions.
            In the case where partial matching is used (using '/' in
            ``event_ids``), ``event_ids`` will be matched according to the
            provided tags, that is, processing works as if the event_ids
            matched by the provided tags had been supplied instead.
            The event_ids must identify nonoverlapping subsets of the epochs.
        method : str
            If 'truncate', events will be truncated from the end of each event
            list. If 'mintime', timing differences between each event list
            will be minimized.

        Returns
        -------
        epochs : instance of Epochs
            The modified Epochs instance.
        indices : array of int
            Indices from the original events list that were dropped.

        Notes
        -----
        For example (if epochs.event_id was {'Left': 1, 'Right': 2,
        'Nonspatial':3}:

            epochs.equalize_event_counts([['Left', 'Right'], 'Nonspatial'])

        would equalize the number of trials in the 'Nonspatial' condition with
        the total number of trials in the 'Left' and 'Right' conditions.

        If multiple indices are provided (e.g. 'Left' and 'Right' in the
        example above), it is not guaranteed that after equalization, the
        conditions will contribute evenly. E.g., it is possible to end up
        with 70 'Nonspatial' trials, 69 'Left' and 1 'Right'.
        """
        if len(event_ids) == 0:
            raise ValueError('event_ids must have at least one element')
        if not self._bad_dropped:
            self.drop_bad()
        # figure out how to equalize
        eq_inds = list()

        # deal with hierarchical tags
        ids = self.event_id
        orig_ids = list(event_ids)
        tagging = False
        if "/" in "".join(ids):
            # make string inputs a list of length 1
            event_ids = [[x] if isinstance(x, str) else x
                         for x in event_ids]
            for ids_ in event_ids:  # check if tagging is attempted
                if any([id_ not in ids for id_ in ids_]):
                    tagging = True
            # 1. treat everything that's not in event_id as a tag
            # 2a. for tags, find all the event_ids matched by the tags
            # 2b. for non-tag ids, just pass them directly
            # 3. do this for every input
            event_ids = [[k for k in ids
                          if all((tag in k.split("/")
                                  for tag in id_))]  # ids matching all tags
                         if all(id__ not in ids for id__ in id_)
                         else id_  # straight pass for non-tag inputs
                         for id_ in event_ids]
            for ii, id_ in enumerate(event_ids):
                if len(id_) == 0:
                    raise KeyError(orig_ids[ii] + "not found in the "
                                   "epoch object's event_id.")
                elif len({sub_id in ids for sub_id in id_}) != 1:
                    err = ("Don't mix hierarchical and regular event_ids"
                           " like in \'%s\'." % ", ".join(id_))
                    raise ValueError(err)

            # raise for non-orthogonal tags
            if tagging is True:
                events_ = [set(self[x].events[:, 0]) for x in event_ids]
                doubles = events_[0].intersection(events_[1])
                if len(doubles):
                    raise ValueError("The two sets of epochs are "
                                     "overlapping. Provide an "
                                     "orthogonal selection.")

        for eq in event_ids:
            eq_inds.append(self._keys_to_idx(eq))

        event_times = [self.events[e, 0] for e in eq_inds]
        indices = _get_drop_indices(event_times, method)
        # need to re-index indices
        indices = np.concatenate([e[idx] for e, idx in zip(eq_inds, indices)])
        self.drop(indices, reason='EQUALIZED_COUNT')
        # actually remove the indices
        return self, indices

    @fill_doc
    def to_data_frame(self, picks=None, index=None,
                      scalings=None, copy=True, long_format=False,
                      time_format='ms'):
        """Export data in tabular structure as a pandas DataFrame.

        Channels are converted to columns in the DataFrame. By default,
        additional columns "time", "epoch" (epoch number), and "condition"
        (epoch event description) are added, unless ``index`` is not ``None``
        (in which case the columns specified in ``index`` will be used to form
        the DataFrame's index instead).

        Parameters
        ----------
        %(picks_all)s
        %(df_index_epo)s
            Valid string values are 'time', 'epoch', and 'condition'.
            Defaults to ``None``.
        %(df_scalings)s
        %(df_copy)s
        %(df_longform_epo)s
        %(df_time_format)s

            .. versionadded:: 0.20

        Returns
        -------
        %(df_return)s
        """
        # check pandas once here, instead of in each private utils function
        pd = _check_pandas_installed()  # noqa
        # arg checking
        valid_index_args = ['time', 'epoch', 'condition']
        valid_time_formats = ['ms', 'timedelta']
        index = _check_pandas_index_arguments(index, valid_index_args)
        time_format = _check_time_format(time_format, valid_time_formats)
        # get data
        picks = _picks_to_idx(self.info, picks, 'all', exclude=())
        data = self.get_data()[:, picks, :]
        times = self.times
        n_epochs, n_picks, n_times = data.shape
        data = np.hstack(data).T  # (time*epochs) x signals
        if copy:
            data = data.copy()
        data = _scale_dataframe_data(self, data, picks, scalings)
        # prepare extra columns / multiindex
        mindex = list()
        times = np.tile(times, n_epochs)
        times = _convert_times(self, times, time_format)
        mindex.append(('time', times))
        rev_event_id = {v: k for k, v in self.event_id.items()}
        conditions = [rev_event_id[k] for k in self.events[:, 2]]
        mindex.append(('condition', np.repeat(conditions, n_times)))
        mindex.append(('epoch', np.repeat(self.selection, n_times)))
        assert all(len(mdx) == len(mindex[0]) for mdx in mindex)
        # build DataFrame
        df = _build_data_frame(self, data, picks, long_format, mindex, index,
                               default_index=['condition', 'epoch', 'time'])
        return df

    def as_type(self, ch_type='grad', mode='fast'):
        """Compute virtual epochs using interpolated fields.

        .. Warning:: Using virtual epochs to compute inverse can yield
            unexpected results. The virtual channels have ``'_v'`` appended
            at the end of the names to emphasize that the data contained in
            them are interpolated.

        Parameters
        ----------
        ch_type : str
            The destination channel type. It can be 'mag' or 'grad'.
        mode : str
            Either ``'accurate'`` or ``'fast'``, determines the quality of the
            Legendre polynomial expansion used. ``'fast'`` should be sufficient
            for most applications.

        Returns
        -------
        epochs : instance of mne.EpochsArray
            The transformed epochs object containing only virtual channels.

        Notes
        -----
        This method returns a copy and does not modify the data it
        operates on. It also returns an EpochsArray instance.

        .. versionadded:: 0.20.0
        """
        from .forward import _as_meg_type_inst
        return _as_meg_type_inst(self, ch_type=ch_type, mode=mode)


def _drop_log_stats(drop_log, ignore=('IGNORED',)):
    """Compute drop log stats.

    Parameters
    ----------
    drop_log : list of list
        Epoch drop log from Epochs.drop_log.
    ignore : list
        The drop reasons to ignore.

    Returns
    -------
    perc : float
        Total percentage of epochs dropped.
    """
    if not isinstance(drop_log, tuple) or \
            not all(isinstance(d, tuple) for d in drop_log) or \
            not all(isinstance(s, str) for d in drop_log for s in d):
        raise TypeError('drop_log must be a tuple of tuple of str')
    perc = 100 * np.mean([len(d) > 0 for d in drop_log
                          if not any(r in ignore for r in d)])
    return perc


def make_metadata(events, event_id, tmin, tmax, sfreq,
                  row_events=None, keep_first=None, keep_last=None):
    """Generate metadata from events for use with `mne.Epochs`.

    This function mimics the epoching process (it constructs time windows
    around time-locked "events of interest") and collates information about
    any other events that occurred within those time windows. The information
    is returned as a :class:`pandas.DataFrame` suitable for use as
    `~mne.Epochs` metadata: one row per time-locked event, and columns
    indicating presence/absence and latency of each ancillary event type.

    The function will also return a new ``events`` array and ``event_id``
    dictionary that correspond to the generated metadata.

    Parameters
    ----------
    events : array, shape (m, 3)
        The :term:`events array <events>`. By default, the returned metadata
        :class:`~pandas.DataFrame` will have as many rows as the events array.
        To create rows for only a subset of events, pass the ``row_events``
        parameter.
    event_id : dict
        A mapping from event names (keys) to event IDs (values). The event
        names will be incorporated as columns of the returned metadata
        :class:`~pandas.DataFrame`.
    tmin, tmax : float
        Start and end of the time interval for metadata generation in seconds,
        relative to the time-locked event of the respective time window.

        .. note::
           If you are planning to attach the generated metadata to
           `~mne.Epochs` and intend to include only events that fall inside
           your epochs time interval, pass the same ``tmin`` and ``tmax``
           values here as you use for your epochs.

    sfreq : float
        The sampling frequency of the data from which the events array was
        extracted.
    row_events : list of str | str | None
        Event types around which to create the time windows / for which to
        create **rows** in the returned metadata :class:`pandas.DataFrame`. If
        provided, the string(s) must be keys of ``event_id``. If ``None``
        (default), rows are created for **all** event types present in
        ``event_id``.
    keep_first : str | list of str | None
        Specify subsets of :term:`hierarchical event descriptors` (HEDs,
        inspired by :footcite:`BigdelyShamloEtAl2013`) matching events of which
        the **first occurrence** within each time window shall be stored in
        addition to the original events.

        .. note::
           There is currently no way to retain **all** occurrences of a
           repeated event. The ``keep_first`` parameter can be used to specify
           subsets of HEDs, effectively creating a new event type that is the
           union of all events types described by the matching HED pattern.
           Only the very first event of this set will be kept.

        For example, you might have two response events types,
        ``response/left`` and ``response/right``; and in trials with both
        responses occurring, you want to keep only the first response. In this
        case, you can pass ``keep_first='response'``. This will add two new
        columns to the metadata: ``response``, indicating at what **time** the
        event  occurred, relative to the time-locked event; and
        ``first_response``, stating which **type** (``'left'`` or ``'right'``)
        of event occurred.
        To match specific subsets of HEDs describing different sets of events,
        pass a list of these subsets, e.g.
        ``keep_first=['response', 'stimulus']``. If ``None`` (default), no
        event aggregation will take place and no new columns will be created.

        .. note::
           By default, this function will always retain  the first instance
           of any event in each time window. For example, if a time window
           contains two ``'response'`` events, the generated ``response``
           column will automatically refer to the first of the two events. In
           this specific case, it is therefore **not** necessary to make use of
           the ``keep_first`` parameter – unless you need to differentiate
           between two types of responses, like in the example above.

    keep_last : list of str | None
        Same as ``keep_first``, but for keeping only the **last**  occurrence
        of matching events. The column indicating the **type** of an event
        ``myevent`` will be named ``last_myevent``.

    Returns
    -------
    metadata : pandas.DataFrame
        Metadata for each row event, with the following columns:

        - ``event_name``, with strings indicating the name of the time-locked
          event ("row event") for that specific time window

        - one column per event type in ``event_id``, with the same name; floats
          indicating the latency of the event in seconds, relative to the
          time-locked event

        - if applicable, additional columns named after the ``keep_first`` and
          ``keep_last`` event types; floats indicating the latency  of the
          event in seconds, relative to the time-locked event

        - if applicable, additional columns ``first_{event_type}`` and
          ``last_{event_type}`` for ``keep_first`` and ``keep_last`` event
          types, respetively; the values will be strings indicating which event
          types were matched by the provided HED patterns

    events : array, shape (n, 3)
        The events corresponding to the generated metadata, i.e. one
        time-locked event per row.
    event_id : dict
        The event dictionary corresponding to the new events array. This will
        be identical to the input dictionary unless ``row_events`` is supplied,
        in which case it will only contain the events provided there.

    Notes
    -----
    The time window used for metadata generation need not correspond to the
    time window used to create the `~mne.Epochs`, to which the metadata will
    be attached; it may well be much shorter or longer, or not overlap at all,
    if desired. The can be useful, for example, to include events that ccurred
    before or after an epoch, e.g. during the inter-trial interval.

    .. versionadded:: 0.23

    References
    ----------
    .. footbibliography::
    """
    from .utils.mixin import _hid_match
    pd = _check_pandas_installed()

    _validate_type(event_id, types=(dict,), item_name='event_id')
    _validate_type(row_events, types=(None, str, list, tuple),
                   item_name='row_events')
    _validate_type(keep_first, types=(None, str, list, tuple),
                   item_name='keep_first')
    _validate_type(keep_last, types=(None, str, list, tuple),
                   item_name='keep_last')

    if not event_id:
        raise ValueError('event_id dictionary must contain at least one entry')

    def _ensure_list(x):
        if x is None:
            return []
        elif isinstance(x, str):
            return [x]
        else:
            return list(x)

    row_events = _ensure_list(row_events)
    keep_first = _ensure_list(keep_first)
    keep_last = _ensure_list(keep_last)

    keep_first_and_last = set(keep_first) & set(keep_last)
    if keep_first_and_last:
        raise ValueError(f'The event names in keep_first and keep_last must '
                         f'be mutually exclusive. Specified in both: '
                         f'{", ".join(sorted(keep_first_and_last))}')
    del keep_first_and_last

    for param_name, values in dict(keep_first=keep_first,
                                   keep_last=keep_last).items():
        for first_last_event_name in values:
            try:
                _hid_match(event_id, [first_last_event_name])
            except KeyError:
                raise ValueError(
                    f'Event "{first_last_event_name}", specified in '
                    f'{param_name}, cannot be found in event_id dictionary')

    event_name_diff = sorted(set(row_events) - set(event_id.keys()))
    if event_name_diff:
        raise ValueError(
            f'Present in row_events, but missing from event_id: '
            f'{", ".join(event_name_diff)}')
    del event_name_diff

    # First and last sample of each epoch, relative to the time-locked event
    # This follows the approach taken in mne.Epochs
    start_sample = int(round(tmin * sfreq))
    stop_sample = int(round(tmax * sfreq)) + 1

    # Make indexing easier
    # We create the DataFrame before subsetting the events so we end up with
    # indices corresponding to the original event indices. Not used for now,
    # but might come in handy sometime later
    events_df = pd.DataFrame(events, columns=('sample', 'prev_id', 'id'))
    id_to_name_map = {v: k for k, v in event_id.items()}

    # Only keep events that are of interest
    events = events[np.in1d(events[:, 2], list(event_id.values()))]
    events_df = events_df.loc[events_df['id'].isin(event_id.values()), :]

    # Prepare & condition the metadata DataFrame

    # Avoid column name duplications if the exact same event name appears in
    # event_id.keys() and keep_first / keep_last simultaneously
    keep_first_cols = [col for col in keep_first if col not in event_id]
    keep_last_cols = [col for col in keep_last if col not in event_id]
    first_cols = [f'first_{col}' for col in keep_first_cols]
    last_cols = [f'last_{col}' for col in keep_last_cols]

    columns = ['event_name',
               *event_id.keys(),
               *keep_first_cols,
               *keep_last_cols,
               *first_cols,
               *last_cols]

    data = np.empty((len(events_df), len(columns)))
    metadata = pd.DataFrame(data=data, columns=columns, index=events_df.index)

    # Event names
    metadata.iloc[:, 0] = ''

    # Event times
    start_idx = 1
    stop_idx = (start_idx + len(event_id.keys()) +
                len(keep_first_cols + keep_last_cols))
    metadata.iloc[:, start_idx:stop_idx] = np.nan

    # keep_first and keep_last names
    start_idx = stop_idx
    metadata.iloc[:, start_idx:] = None

    # We're all set, let's iterate over all eventns and fill in in the
    # respective cells in the metadata. We will subset this to include only
    # `row_events` later
    for row_event in events_df.itertuples(name='RowEvent'):
        row_idx = row_event.Index
        metadata.loc[row_idx, 'event_name'] = \
            id_to_name_map[row_event.id]

        # Determine which events fall into the current epoch
        window_start_sample = row_event.sample + start_sample
        window_stop_sample = row_event.sample + stop_sample
        events_in_window = events_df.loc[
            (events_df['sample'] >= window_start_sample) &
            (events_df['sample'] <= window_stop_sample), :]

        assert not events_in_window.empty

        # Store the metadata
        for event in events_in_window.itertuples(name='Event'):
            event_sample = event.sample - row_event.sample
            event_time = event_sample / sfreq
            event_time = 0 if np.isclose(event_time, 0) else event_time
            event_name = id_to_name_map[event.id]

            if not np.isnan(metadata.loc[row_idx, event_name]):
                # Event already exists in current time window!
                assert metadata.loc[row_idx, event_name] <= event_time

                if event_name not in keep_last:
                    continue

            metadata.loc[row_idx, event_name] = event_time

            # Handle keep_first and keep_last event aggregation
            for event_group_name in keep_first + keep_last:
                if event_name not in _hid_match(event_id, [event_group_name]):
                    continue

                if event_group_name in keep_first:
                    first_last_col = f'first_{event_group_name}'
                else:
                    first_last_col = f'last_{event_group_name}'

                old_time = metadata.loc[row_idx, event_group_name]
                if not np.isnan(old_time):
                    if ((event_group_name in keep_first and
                         old_time <= event_time) or
                        (event_group_name in keep_last and
                         old_time >= event_time)):
                        continue

                if event_group_name not in event_id:
                    # This is an HED. Strip redundant information from the
                    # event name
                    name = (event_name
                            .replace(event_group_name, '')
                            .replace('//', '/')
                            .strip('/'))
                    metadata.loc[row_idx, first_last_col] = name
                    del name

                metadata.loc[row_idx, event_group_name] = event_time

    # Only keep rows of interest
    if row_events:
        event_id_timelocked = {name: val for name, val in event_id.items()
                               if name in row_events}
        events = events[np.in1d(events[:, 2],
                                list(event_id_timelocked.values()))]
        metadata = metadata.loc[
            metadata['event_name'].isin(event_id_timelocked)]
        assert len(events) == len(metadata)
        event_id = event_id_timelocked

    return metadata, events, event_id


@fill_doc
class Epochs(BaseEpochs):
    """Epochs extracted from a Raw instance.

    Parameters
    ----------
    %(epochs_raw)s
    %(epochs_events_event_id)s
    %(epochs_tmin_tmax)s
    %(baseline_epochs)s
        Defaults to ``(None, 0)``, i.e. beginning of the the data until
        time point zero.
    %(picks_all)s
    preload : bool
        %(epochs_preload)s
    %(reject_epochs)s
    %(flat)s
    %(proj_epochs)s
    %(decim)s
    %(epochs_reject_tmin_tmax)s
    %(epochs_detrend)s
    %(epochs_on_missing)s
    %(reject_by_annotation_epochs)s
    %(epochs_metadata)s
    %(epochs_event_repeated)s
    %(verbose)s

    Attributes
    ----------
    info : instance of Info
        Measurement info.
    event_id : dict
        Names of conditions corresponding to event_ids.
    ch_names : list of string
        List of channel names.
    selection : array
        List of indices of selected events (not dropped or ignored etc.). For
        example, if the original event array had 4 events and the second event
        has been dropped, this attribute would be np.array([0, 2, 3]).
    preload : bool
        Indicates whether epochs are in memory.
    drop_log : tuple of tuple
        A tuple of the same length as the event array used to initialize the
        Epochs object. If the i-th original event is still part of the
        selection, drop_log[i] will be an empty tuple; otherwise it will be
        a tuple of the reasons the event is not longer in the selection, e.g.:

        - 'IGNORED'
            If it isn't part of the current subset defined by the user
        - 'NO_DATA' or 'TOO_SHORT'
            If epoch didn't contain enough data names of channels that exceeded
            the amplitude threshold
        - 'EQUALIZED_COUNTS'
            See :meth:`~mne.Epochs.equalize_event_counts`
        - 'USER'
            For user-defined reasons (see :meth:`~mne.Epochs.drop`).
    filename : str
        The filename of the object.
    times :  ndarray
        Time vector in seconds. Goes from ``tmin`` to ``tmax``. Time interval
        between consecutive time samples is equal to the inverse of the
        sampling frequency.
    %(verbose)s

    See Also
    --------
    mne.epochs.combine_event_ids
    mne.Epochs.equalize_event_counts

    Notes
    -----
    When accessing data, Epochs are detrended, baseline-corrected, and
    decimated, then projectors are (optionally) applied.

    For indexing and slicing using ``epochs[...]``, see
    :meth:`mne.Epochs.__getitem__`.

    All methods for iteration over objects (using :meth:`mne.Epochs.__iter__`,
    :meth:`mne.Epochs.iter_evoked` or :meth:`mne.Epochs.next`) use the same
    internal state.

    If ``event_repeated`` is set to ``'merge'``, the coinciding events
    (duplicates) will be merged into a single event_id and assigned a new
    id_number as::

        event_id['{event_id_1}/{event_id_2}/...'] = new_id_number

    For example with the event_id ``{'aud': 1, 'vis': 2}`` and the events
    ``[[0, 0, 1], [0, 0, 2]]``, the "merge" behavior will update both event_id
    and events to be: ``{'aud/vis': 3}`` and ``[[0, 0, 3]]`` respectively.
    """

    @verbose
    def __init__(self, raw, events, event_id=None, tmin=-0.2, tmax=0.5,
                 baseline=(None, 0), picks=None, preload=False, reject=None,
                 flat=None, proj=True, decim=1, reject_tmin=None,
                 reject_tmax=None, detrend=None, on_missing='raise',
                 reject_by_annotation=True, metadata=None,
                 event_repeated='error', verbose=None):  # noqa: D102
        if not isinstance(raw, BaseRaw):
            raise ValueError('The first argument to `Epochs` must be an '
                             'instance of mne.io.BaseRaw')
        info = deepcopy(raw.info)

        # proj is on when applied in Raw
        proj = proj or raw.proj

        self.reject_by_annotation = reject_by_annotation
        # call BaseEpochs constructor
        super(Epochs, self).__init__(
            info, None, events, event_id, tmin, tmax, metadata=metadata,
            baseline=baseline, raw=raw, picks=picks, reject=reject,
            flat=flat, decim=decim, reject_tmin=reject_tmin,
            reject_tmax=reject_tmax, detrend=detrend,
            proj=proj, on_missing=on_missing, preload_at_end=preload,
            event_repeated=event_repeated, verbose=verbose)

    @verbose
    def _get_epoch_from_raw(self, idx, verbose=None):
        """Load one epoch from disk.

        Returns
        -------
        data : array | str | None
            If string, it's details on rejection reason.
            If array, it's the data in the desired range (good segment)
            If None, it means no data is available.
        """
        if self._raw is None:
            # This should never happen, as raw=None only if preload=True
            raise ValueError('An error has occurred, no valid raw file found. '
                             'Please report this to the mne-python '
                             'developers.')
        sfreq = self._raw.info['sfreq']
        event_samp = self.events[idx, 0]
        # Read a data segment from "start" to "stop" in samples
        first_samp = self._raw.first_samp
        start = int(round(event_samp + self._raw_times[0] * sfreq))
        start -= first_samp
        stop = start + len(self._raw_times)

        # reject_tmin, and reject_tmax need to be converted to samples to
        # check the reject_by_annotation boundaries: reject_start, reject_stop
        reject_tmin = self.reject_tmin
        if reject_tmin is None:
            reject_tmin = self._raw_times[0]
        reject_start = int(round(event_samp + reject_tmin * sfreq))
        reject_start -= first_samp

        reject_tmax = self.reject_tmax
        if reject_tmax is None:
            reject_tmax = self._raw_times[-1]
        diff = int(round((self._raw_times[-1] - reject_tmax) * sfreq))
        reject_stop = stop - diff

        logger.debug('    Getting epoch for %d-%d' % (start, stop))
        data = self._raw._check_bad_segment(start, stop, self.picks,
                                            reject_start, reject_stop,
                                            self.reject_by_annotation)
        return data


@fill_doc
class EpochsArray(BaseEpochs):
    """Epochs object from numpy array.

    Parameters
    ----------
    data : array, shape (n_epochs, n_channels, n_times)
        The channels' time series for each epoch. See notes for proper units of
        measure.
    info : instance of Info
        Info dictionary. Consider using ``create_info`` to populate
        this structure.
    events : None | array of int, shape (n_events, 3)
        The events typically returned by the read_events function.
        If some events don't match the events of interest as specified
        by event_id, they will be marked as 'IGNORED' in the drop log.
        If None (default), all event values are set to 1 and event time-samples
        are set to range(n_epochs).
    tmin : float
        Start time before event. If nothing provided, defaults to 0.
    event_id : int | list of int | dict | None
        The id of the event to consider. If dict,
        the keys can later be used to access associated events. Example:
        dict(auditory=1, visual=3). If int, a dict will be created with
        the id as string. If a list, all events with the IDs specified
        in the list are used. If None, all events will be used with
        and a dict is created with string integer names corresponding
        to the event id integers.
    %(reject_epochs)s
    %(flat)s
    reject_tmin : scalar | None
        Start of the time window used to reject epochs (with the default None,
        the window will start with tmin).
    reject_tmax : scalar | None
        End of the time window used to reject epochs (with the default None,
        the window will end with tmax).
    %(baseline_epochs)s
        Defaults to ``None``, i.e. no baseline correction.
    proj : bool | 'delayed'
        Apply SSP projection vectors. See :class:`mne.Epochs` for details.
    on_missing : str
        See :class:`mne.Epochs` docstring for details.
    metadata : instance of pandas.DataFrame | None
        See :class:`mne.Epochs` docstring for details.

        .. versionadded:: 0.16
    selection : ndarray | None
        The selection compared to the original set of epochs.
        Can be None to use ``np.arange(len(events))``.

        .. versionadded:: 0.16
    %(verbose)s

    See Also
    --------
    create_info
    EvokedArray
    io.RawArray

    Notes
    -----
    Proper units of measure:

    * V: eeg, eog, seeg, dbs, emg, ecg, bio, ecog
    * T: mag
    * T/m: grad
    * M: hbo, hbr
    * Am: dipole
    * AU: misc
    """

    @verbose
    def __init__(self, data, info, events=None, tmin=0, event_id=None,
                 reject=None, flat=None, reject_tmin=None,
                 reject_tmax=None, baseline=None, proj=True,
                 on_missing='raise', metadata=None, selection=None,
                 verbose=None):  # noqa: D102
        dtype = np.complex128 if np.any(np.iscomplex(data)) else np.float64
        data = np.asanyarray(data, dtype=dtype)
        if data.ndim != 3:
            raise ValueError('Data must be a 3D array of shape (n_epochs, '
                             'n_channels, n_samples)')

        if len(info['ch_names']) != data.shape[1]:
            raise ValueError('Info and data must have same number of '
                             'channels.')
        if events is None:
            n_epochs = len(data)
            events = _gen_events(n_epochs)
        info = info.copy()  # do not modify original info
        tmax = (data.shape[2] - 1) / info['sfreq'] + tmin
        super(EpochsArray, self).__init__(
            info, data, events, event_id, tmin, tmax, baseline, reject=reject,
            flat=flat, reject_tmin=reject_tmin, reject_tmax=reject_tmax,
            decim=1, metadata=metadata, selection=selection, proj=proj,
            on_missing=on_missing)
        if self.baseline is not None:
            self._do_baseline = True
        if len(events) != np.in1d(self.events[:, 2],
                                  list(self.event_id.values())).sum():
            raise ValueError('The events must only contain event numbers from '
                             'event_id')
        detrend_picks = self._detrend_picks
        for e in self._data:
            # This is safe without assignment b/c there is no decim
            self._detrend_offset_decim(e, detrend_picks)
        self.drop_bad()


def combine_event_ids(epochs, old_event_ids, new_event_id, copy=True):
    """Collapse event_ids from an epochs instance into a new event_id.

    Parameters
    ----------
    epochs : instance of Epochs
        The epochs to operate on.
    old_event_ids : str, or list
        Conditions to collapse together.
    new_event_id : dict, or int
        A one-element dict (or a single integer) for the new
        condition. Note that for safety, this cannot be any
        existing id (in epochs.event_id.values()).
    copy : bool
        Whether to return a new instance or modify in place.

    Returns
    -------
    epochs : instance of Epochs
        The modified epochs.

    Notes
    -----
    This For example (if epochs.event_id was ``{'Left': 1, 'Right': 2}``::

        combine_event_ids(epochs, ['Left', 'Right'], {'Directional': 12})

    would create a 'Directional' entry in epochs.event_id replacing
    'Left' and 'Right' (combining their trials).
    """
    epochs = epochs.copy() if copy else epochs
    old_event_ids = np.asanyarray(old_event_ids)
    if isinstance(new_event_id, int):
        new_event_id = {str(new_event_id): new_event_id}
    else:
        if not isinstance(new_event_id, dict):
            raise ValueError('new_event_id must be a dict or int')
        if not len(list(new_event_id.keys())) == 1:
            raise ValueError('new_event_id dict must have one entry')
    new_event_num = list(new_event_id.values())[0]
    new_event_num = operator.index(new_event_num)
    if new_event_num in epochs.event_id.values():
        raise ValueError('new_event_id value must not already exist')
    # could use .pop() here, but if a latter one doesn't exist, we're
    # in trouble, so run them all here and pop() later
    old_event_nums = np.array([epochs.event_id[key] for key in old_event_ids])
    # find the ones to replace
    inds = np.any(epochs.events[:, 2][:, np.newaxis] ==
                  old_event_nums[np.newaxis, :], axis=1)
    # replace the event numbers in the events list
    epochs.events[inds, 2] = new_event_num
    # delete old entries
    for key in old_event_ids:
        epochs.event_id.pop(key)
    # add the new entry
    epochs.event_id.update(new_event_id)
    return epochs


def equalize_epoch_counts(epochs_list, method='mintime'):
    """Equalize the number of trials in multiple Epoch instances.

    Parameters
    ----------
    epochs_list : list of Epochs instances
        The Epochs instances to equalize trial counts for.
    method : str
        If 'truncate', events will be truncated from the end of each event
        list. If 'mintime', timing differences between each event list will be
        minimized.

    Notes
    -----
    This tries to make the remaining epochs occurring as close as possible in
    time. This method works based on the idea that if there happened to be some
    time-varying (like on the scale of minutes) noise characteristics during
    a recording, they could be compensated for (to some extent) in the
    equalization process. This method thus seeks to reduce any of those effects
    by minimizing the differences in the times of the events in the two sets of
    epochs. For example, if one had event times [1, 2, 3, 4, 120, 121] and the
    other one had [3.5, 4.5, 120.5, 121.5], it would remove events at times
    [1, 2] in the first epochs and not [120, 121].

    Examples
    --------
    >>> equalize_epoch_counts([epochs1, epochs2])  # doctest: +SKIP
    """
    if not all(isinstance(e, BaseEpochs) for e in epochs_list):
        raise ValueError('All inputs must be Epochs instances')

    # make sure bad epochs are dropped
    for e in epochs_list:
        if not e._bad_dropped:
            e.drop_bad()
    event_times = [e.events[:, 0] for e in epochs_list]
    indices = _get_drop_indices(event_times, method)
    for e, inds in zip(epochs_list, indices):
        e.drop(inds, reason='EQUALIZED_COUNT')


def _get_drop_indices(event_times, method):
    """Get indices to drop from multiple event timing lists."""
    small_idx = np.argmin([e.shape[0] for e in event_times])
    small_e_times = event_times[small_idx]
    _check_option('method', method, ['mintime', 'truncate'])
    indices = list()
    for e in event_times:
        if method == 'mintime':
            mask = _minimize_time_diff(small_e_times, e)
        else:
            mask = np.ones(e.shape[0], dtype=bool)
            mask[small_e_times.shape[0]:] = False
        indices.append(np.where(np.logical_not(mask))[0])

    return indices


def _minimize_time_diff(t_shorter, t_longer):
    """Find a boolean mask to minimize timing differences."""
    from scipy.interpolate import interp1d
    keep = np.ones((len(t_longer)), dtype=bool)
    if len(t_shorter) == 0:
        keep.fill(False)
        return keep
    scores = np.ones((len(t_longer)))
    x1 = np.arange(len(t_shorter))
    # The first set of keep masks to test
    kwargs = dict(copy=False, bounds_error=False)
    # this is a speed tweak, only exists for certain versions of scipy
    if 'assume_sorted' in _get_args(interp1d.__init__):
        kwargs['assume_sorted'] = True
    shorter_interp = interp1d(x1, t_shorter, fill_value=t_shorter[-1],
                              **kwargs)
    for ii in range(len(t_longer) - len(t_shorter)):
        scores.fill(np.inf)
        # set up the keep masks to test, eliminating any rows that are already
        # gone
        keep_mask = ~np.eye(len(t_longer), dtype=bool)[keep]
        keep_mask[:, ~keep] = False
        # Check every possible removal to see if it minimizes
        x2 = np.arange(len(t_longer) - ii - 1)
        t_keeps = np.array([t_longer[km] for km in keep_mask])
        longer_interp = interp1d(x2, t_keeps, axis=1,
                                 fill_value=t_keeps[:, -1],
                                 **kwargs)
        d1 = longer_interp(x1) - t_shorter
        d2 = shorter_interp(x2) - t_keeps
        scores[keep] = np.abs(d1, d1).sum(axis=1) + np.abs(d2, d2).sum(axis=1)
        keep[np.argmin(scores)] = False
    return keep


@verbose
def _is_good(e, ch_names, channel_type_idx, reject, flat, full_report=False,
             ignore_chs=[], verbose=None):
    """Test if data segment e is good according to reject and flat.

    If full_report=True, it will give True/False as well as a list of all
    offending channels.
    """
    bad_tuple = tuple()
    has_printed = False
    checkable = np.ones(len(ch_names), dtype=bool)
    checkable[np.array([c in ignore_chs
                        for c in ch_names], dtype=bool)] = False
    for refl, f, t in zip([reject, flat], [np.greater, np.less], ['', 'flat']):
        if refl is not None:
            for key, thresh in refl.items():
                idx = channel_type_idx[key]
                name = key.upper()
                if len(idx) > 0:
                    e_idx = e[idx]
                    deltas = np.max(e_idx, axis=1) - np.min(e_idx, axis=1)
                    checkable_idx = checkable[idx]
                    idx_deltas = np.where(np.logical_and(f(deltas, thresh),
                                                         checkable_idx))[0]

                    if len(idx_deltas) > 0:
                        bad_names = [ch_names[idx[i]] for i in idx_deltas]
                        if (not has_printed):
                            logger.info('    Rejecting %s epoch based on %s : '
                                        '%s' % (t, name, bad_names))
                            has_printed = True
                        if not full_report:
                            return False
                        else:
                            bad_tuple += tuple(bad_names)

    if not full_report:
        return True
    else:
        if bad_tuple == ():
            return True, None
        else:
            return False, bad_tuple


def _read_one_epoch_file(f, tree, preload):
    """Read a single FIF file."""
    with f as fid:
        #   Read the measurement info
        info, meas = read_meas_info(fid, tree, clean_bads=True)

        events, mappings = _read_events_fif(fid, tree)

        #   Metadata
        metadata = None
        metadata_tree = dir_tree_find(tree, FIFF.FIFFB_MNE_METADATA)
        if len(metadata_tree) > 0:
            for dd in metadata_tree[0]['directory']:
                kind = dd.kind
                pos = dd.pos
                if kind == FIFF.FIFF_DESCRIPTION:
                    metadata = read_tag(fid, pos).data
                    metadata = _prepare_read_metadata(metadata)
                    break

        #   Locate the data of interest
        processed = dir_tree_find(meas, FIFF.FIFFB_PROCESSED_DATA)
        del meas
        if len(processed) == 0:
            raise ValueError('Could not find processed data')

        epochs_node = dir_tree_find(tree, FIFF.FIFFB_MNE_EPOCHS)
        if len(epochs_node) == 0:
            # before version 0.11 we errantly saved with this tag instead of
            # an MNE tag
            epochs_node = dir_tree_find(tree, FIFF.FIFFB_MNE_EPOCHS)
            if len(epochs_node) == 0:
                epochs_node = dir_tree_find(tree, 122)  # 122 used before v0.11
                if len(epochs_node) == 0:
                    raise ValueError('Could not find epochs data')

        my_epochs = epochs_node[0]

        # Now find the data in the block
        data = None
        data_tag = None
        bmin, bmax = None, None
        baseline = None
        selection = None
        drop_log = None
        reject_params = {}
        for k in range(my_epochs['nent']):
            kind = my_epochs['directory'][k].kind
            pos = my_epochs['directory'][k].pos
            if kind == FIFF.FIFF_FIRST_SAMPLE:
                tag = read_tag(fid, pos)
                first = int(tag.data)
            elif kind == FIFF.FIFF_LAST_SAMPLE:
                tag = read_tag(fid, pos)
                last = int(tag.data)
            elif kind == FIFF.FIFF_EPOCH:
                # delay reading until later
                fid.seek(pos, 0)
                data_tag = read_tag_info(fid)
                data_tag.pos = pos
                data_tag.type = data_tag.type ^ (1 << 30)
            elif kind in [FIFF.FIFF_MNE_BASELINE_MIN, 304]:
                # Constant 304 was used before v0.11
                tag = read_tag(fid, pos)
                bmin = float(tag.data)
            elif kind in [FIFF.FIFF_MNE_BASELINE_MAX, 305]:
                # Constant 305 was used before v0.11
                tag = read_tag(fid, pos)
                bmax = float(tag.data)
            elif kind == FIFF.FIFF_MNE_EPOCHS_SELECTION:
                tag = read_tag(fid, pos)
                selection = np.array(tag.data)
            elif kind == FIFF.FIFF_MNE_EPOCHS_DROP_LOG:
                tag = read_tag(fid, pos)
                drop_log = tag.data
                drop_log = json.loads(drop_log)
                drop_log = tuple(tuple(x) for x in drop_log)
            elif kind == FIFF.FIFF_MNE_EPOCHS_REJECT_FLAT:
                tag = read_tag(fid, pos)
                reject_params = json.loads(tag.data)

        if bmin is not None or bmax is not None:
            baseline = (bmin, bmax)

        n_samp = last - first + 1
        logger.info('    Found the data of interest:')
        logger.info('        t = %10.2f ... %10.2f ms'
                    % (1000 * first / info['sfreq'],
                       1000 * last / info['sfreq']))
        if info['comps'] is not None:
            logger.info('        %d CTF compensation matrices available'
                        % len(info['comps']))

        # Inspect the data
        if data_tag is None:
            raise ValueError('Epochs data not found')
        epoch_shape = (len(info['ch_names']), n_samp)
        size_expected = len(events) * np.prod(epoch_shape)
        # on read double-precision is always used
        if data_tag.type == FIFF.FIFFT_FLOAT:
            datatype = np.float64
            fmt = '>f4'
        elif data_tag.type == FIFF.FIFFT_DOUBLE:
            datatype = np.float64
            fmt = '>f8'
        elif data_tag.type == FIFF.FIFFT_COMPLEX_FLOAT:
            datatype = np.complex128
            fmt = '>c8'
        elif data_tag.type == FIFF.FIFFT_COMPLEX_DOUBLE:
            datatype = np.complex128
            fmt = '>c16'
        fmt_itemsize = np.dtype(fmt).itemsize
        assert fmt_itemsize in (4, 8, 16)
        size_actual = data_tag.size // fmt_itemsize - 16 // fmt_itemsize

        if not size_actual == size_expected:
            raise ValueError('Incorrect number of samples (%d instead of %d)'
                             % (size_actual, size_expected))

        # Calibration factors
        cals = np.array([[info['chs'][k]['cal'] *
                          info['chs'][k].get('scale', 1.0)]
                         for k in range(info['nchan'])], np.float64)

        # Read the data
        if preload:
            data = read_tag(fid, data_tag.pos).data.astype(datatype)
            data *= cals

        # Put it all together
        tmin = first / info['sfreq']
        tmax = last / info['sfreq']
        event_id = ({str(e): e for e in np.unique(events[:, 2])}
                    if mappings is None else mappings)
        # In case epochs didn't have a FIFF.FIFF_MNE_EPOCHS_SELECTION tag
        # (version < 0.8):
        if selection is None:
            selection = np.arange(len(events))
        if drop_log is None:
            drop_log = ((),) * len(events)

    return (info, data, data_tag, events, event_id, metadata, tmin, tmax,
            baseline, selection, drop_log, epoch_shape, cals, reject_params,
            fmt)


@verbose
def read_epochs(fname, proj=True, preload=True, verbose=None):
    """Read epochs from a fif file.

    Parameters
    ----------
    fname : str | file-like
        The epochs filename to load. Filename should end with -epo.fif or
        -epo.fif.gz. If a file-like object is provided, preloading must be
        used.
    %(proj_epochs)s
    preload : bool
        If True, read all epochs from disk immediately. If False, epochs will
        be read on demand.
    %(verbose)s

    Returns
    -------
    epochs : instance of Epochs
        The epochs.
    """
    return EpochsFIF(fname, proj, preload, verbose)


class _RawContainer(object):
    """Helper for a raw data container."""

    def __init__(self, fid, data_tag, event_samps, epoch_shape,
                 cals, fmt):  # noqa: D102
        self.fid = fid
        self.data_tag = data_tag
        self.event_samps = event_samps
        self.epoch_shape = epoch_shape
        self.cals = cals
        self.proj = False
        self.fmt = fmt

    def __del__(self):  # noqa: D105
        self.fid.close()


@fill_doc
class EpochsFIF(BaseEpochs):
    """Epochs read from disk.

    Parameters
    ----------
    fname : str | file-like
        The name of the file, which should end with -epo.fif or -epo.fif.gz. If
        a file-like object is provided, preloading must be used.
    %(proj_epochs)s
    preload : bool
        If True, read all epochs from disk immediately. If False, epochs will
        be read on demand.
    %(verbose)s

    See Also
    --------
    mne.Epochs
    mne.epochs.combine_event_ids
    mne.Epochs.equalize_event_counts
    """

    @verbose
    def __init__(self, fname, proj=True, preload=True,
                 verbose=None):  # noqa: D102
        if isinstance(fname, str):
            check_fname(fname, 'epochs', ('-epo.fif', '-epo.fif.gz',
                                          '_epo.fif', '_epo.fif.gz'))
        elif not preload:
            raise ValueError('preload must be used with file-like objects')

        fnames = [fname]
        ep_list = list()
        raw = list()
        for fname in fnames:
            fname_rep = _get_fname_rep(fname)
            logger.info('Reading %s ...' % fname_rep)
            fid, tree, _ = fiff_open(fname, preload=preload)
            next_fname = _get_next_fname(fid, fname, tree)
            (info, data, data_tag, events, event_id, metadata, tmin, tmax,
             baseline, selection, drop_log, epoch_shape, cals,
             reject_params, fmt) = \
                _read_one_epoch_file(fid, tree, preload)

            if (events[:, 0] < 0).any():
                events = events.copy()
                warn('Incorrect events detected on disk, setting event '
                     'numbers to consecutive increasing integers')
                events[:, 0] = np.arange(1, len(events) + 1)
            # here we ignore missing events, since users should already be
            # aware of missing events if they have saved data that way
            # we also retain original baseline without re-applying baseline
            # correction (data is being baseline-corrected when written to
            # disk)
            epoch = BaseEpochs(
                info, data, events, event_id, tmin, tmax,
                baseline=None,
                metadata=metadata, on_missing='ignore',
                selection=selection, drop_log=drop_log,
                proj=False, verbose=False)
            epoch.baseline = baseline
            epoch._do_baseline = False  # might be superfluous but won't hurt
            ep_list.append(epoch)

            if not preload:
                # store everything we need to index back to the original data
                raw.append(_RawContainer(fiff_open(fname)[0], data_tag,
                                         events[:, 0].copy(), epoch_shape,
                                         cals, fmt))

            if next_fname is not None:
                fnames.append(next_fname)

        (info, data, events, event_id, tmin, tmax, metadata, baseline,
         selection, drop_log, _) = \
            _concatenate_epochs(ep_list, with_data=preload, add_offset=False)
        # we need this uniqueness for non-preloaded data to work properly
        if len(np.unique(events[:, 0])) != len(events):
            raise RuntimeError('Event time samples were not unique')

        # correct the drop log
        assert len(drop_log) % len(fnames) == 0
        step = len(drop_log) // len(fnames)
        offsets = np.arange(step, len(drop_log) + 1, step)
        drop_log = list(drop_log)
        for i1, i2 in zip(offsets[:-1], offsets[1:]):
            other_log = drop_log[i1:i2]
            for k, (a, b) in enumerate(zip(drop_log, other_log)):
                if a == ('IGNORED',) and b != ('IGNORED',):
                    drop_log[k] = b
        drop_log = tuple(drop_log[:step])

        # call BaseEpochs constructor
        # again, ensure we're retaining the baseline period originally loaded
        # from disk without trying to re-apply baseline correction
        super(EpochsFIF, self).__init__(
            info, data, events, event_id, tmin, tmax, baseline=None, raw=raw,
            proj=proj, preload_at_end=False, on_missing='ignore',
            selection=selection, drop_log=drop_log, filename=fname_rep,
            metadata=metadata, verbose=verbose, **reject_params)
        self.baseline = baseline
        self._do_baseline = False
        # use the private property instead of drop_bad so that epochs
        # are not all read from disk for preload=False
        self._bad_dropped = True

    @verbose
    def _get_epoch_from_raw(self, idx, verbose=None):
        """Load one epoch from disk."""
        # Find the right file and offset to use
        event_samp = self.events[idx, 0]
        for raw in self._raw:
            idx = np.where(raw.event_samps == event_samp)[0]
            if len(idx) == 1:
                fmt = raw.fmt
                idx = idx[0]
                size = np.prod(raw.epoch_shape) * np.dtype(fmt).itemsize
                offset = idx * size + 16  # 16 = Tag header
                break
        else:
            # read the correct subset of the data
            raise RuntimeError('Correct epoch could not be found, please '
                               'contact mne-python developers')
        # the following is equivalent to this, but faster:
        #
        # >>> data = read_tag(raw.fid, raw.data_tag.pos).data.astype(float)
        # >>> data *= raw.cals[np.newaxis, :, :]
        # >>> data = data[idx]
        #
        # Eventually this could be refactored in io/tag.py if other functions
        # could make use of it
        raw.fid.seek(raw.data_tag.pos + offset, 0)
        if fmt == '>c8':
            read_fmt = '>f4'
        elif fmt == '>c16':
            read_fmt = '>f8'
        else:
            read_fmt = fmt
        data = np.frombuffer(raw.fid.read(size), read_fmt)
        if read_fmt != fmt:
            data = data.view(fmt)
            data = data.astype(np.complex128)
        else:
            data = data.astype(np.float64)

        data.shape = raw.epoch_shape
        data *= raw.cals
        return data


@fill_doc
def bootstrap(epochs, random_state=None):
    """Compute epochs selected by bootstrapping.

    Parameters
    ----------
    epochs : Epochs instance
        epochs data to be bootstrapped
    %(random_state)s

    Returns
    -------
    epochs : Epochs instance
        The bootstrap samples
    """
    if not epochs.preload:
        raise RuntimeError('Modifying data of epochs is only supported '
                           'when preloading is used. Use preload=True '
                           'in the constructor.')

    rng = check_random_state(random_state)
    epochs_bootstrap = epochs.copy()
    n_events = len(epochs_bootstrap.events)
    idx = rng_uniform(rng)(0, n_events, n_events)
    epochs_bootstrap = epochs_bootstrap[idx]
    return epochs_bootstrap


def _check_merge_epochs(epochs_list):
    """Aux function."""
    if len({tuple(epochs.event_id.items()) for epochs in epochs_list}) != 1:
        raise NotImplementedError("Epochs with unequal values for event_id")
    if len({epochs.tmin for epochs in epochs_list}) != 1:
        raise NotImplementedError("Epochs with unequal values for tmin")
    if len({epochs.tmax for epochs in epochs_list}) != 1:
        raise NotImplementedError("Epochs with unequal values for tmax")
    if len({epochs.baseline for epochs in epochs_list}) != 1:
        raise NotImplementedError("Epochs with unequal values for baseline")


@verbose
def add_channels_epochs(epochs_list, verbose=None):
    """Concatenate channels, info and data from two Epochs objects.

    Parameters
    ----------
    epochs_list : list of Epochs
        Epochs object to concatenate.
    %(verbose)s Defaults to True if any of the input epochs have verbose=True.

    Returns
    -------
    epochs : instance of Epochs
        Concatenated epochs.
    """
    if not all(e.preload for e in epochs_list):
        raise ValueError('All epochs must be preloaded.')

    info = _merge_info([epochs.info for epochs in epochs_list])
    data = [epochs.get_data() for epochs in epochs_list]
    _check_merge_epochs(epochs_list)
    for d in data:
        if len(d) != len(data[0]):
            raise ValueError('all epochs must be of the same length')

    data = np.concatenate(data, axis=1)

    if len(info['chs']) != data.shape[1]:
        err = "Data shape does not match channel number in measurement info"
        raise RuntimeError(err)

    events = epochs_list[0].events.copy()
    all_same = all(np.array_equal(events, epochs.events)
                   for epochs in epochs_list[1:])
    if not all_same:
        raise ValueError('Events must be the same.')

    proj = any(e.proj for e in epochs_list)

    if verbose is None:
        verbose = any(e.verbose for e in epochs_list)

    epochs = epochs_list[0].copy()
    epochs.info = info
    epochs.picks = None
    epochs.verbose = verbose
    epochs.events = events
    epochs.preload = True
    epochs._bad_dropped = True
    epochs._data = data
    epochs._projector, epochs.info = setup_proj(epochs.info, False,
                                                activate=proj)
    return epochs


def _compare_epochs_infos(info1, info2, name):
    """Compare infos."""
    if not isinstance(name, str):  # passed epochs index
        name = f'epochs[{name:d}]'
    info1._check_consistency()
    info2._check_consistency()
    if info1['nchan'] != info2['nchan']:
        raise ValueError(f'{name}.info[\'nchan\'] must match')
    if set(info1['bads']) != set(info2['bads']):
        raise ValueError(f'{name}.info[\'bads\'] must match')
    if info1['sfreq'] != info2['sfreq']:
        raise ValueError(f'{name}.info[\'sfreq\'] must match')
    if set(info1['ch_names']) != set(info2['ch_names']):
        raise ValueError(f'{name}.info[\'ch_names\'] must match')
    if len(info2['projs']) != len(info1['projs']):
        raise ValueError(f'SSP projectors in {name} must be the same')
    if any(not _proj_equal(p1, p2) for p1, p2 in
           zip(info2['projs'], info1['projs'])):
        raise ValueError(f'SSP projectors in {name} must be the same')
    if (info1['dev_head_t'] is None) != (info2['dev_head_t'] is None) or \
            (info1['dev_head_t'] is not None and not
             np.allclose(info1['dev_head_t']['trans'],
                         info2['dev_head_t']['trans'], rtol=1e-6)):
        raise ValueError(f'{name}.info[\'dev_head_t\'] must match. The '
                         'instances probably come from different runs, and '
                         'are therefore associated with different head '
                         'positions. Manually change info[\'dev_head_t\'] to '
                         'avoid this message but beware that this means the '
                         'MEG sensors will not be properly spatially aligned. '
                         'See mne.preprocessing.maxwell_filter to realign the '
                         'runs to a common head position.')


def _update_offset(offset, events, shift):
    if offset == 0:
        return offset
    offset = 0 if offset is None else offset
    offset = np.int64(offset) + np.max(events[:, 0]) + shift
    if offset > INT32_MAX:
        warn(f'Event number greater than {INT32_MAX} created, events[:, 0] '
             'will be assigned consecutive increasing integer values')
        offset = 0
    return offset


def _concatenate_epochs(epochs_list, with_data=True, add_offset=True):
    """Auxiliary function for concatenating epochs."""
    if not isinstance(epochs_list, (list, tuple)):
        raise TypeError('epochs_list must be a list or tuple, got %s'
                        % (type(epochs_list),))
    for ei, epochs in enumerate(epochs_list):
        if not isinstance(epochs, BaseEpochs):
            raise TypeError('epochs_list[%d] must be an instance of Epochs, '
                            'got %s' % (ei, type(epochs)))
    out = epochs_list[0]
    offsets = [0]
    if with_data:
        out.drop_bad()
        offsets.append(len(out))
    events = [out.events]
    metadata = [out.metadata]
    baseline, tmin, tmax = out.baseline, out.tmin, out.tmax
    info = deepcopy(out.info)
    verbose = out.verbose
    drop_log = out.drop_log
    event_id = deepcopy(out.event_id)
    selection = out.selection
    # offset is the last epoch + tmax + 10 second
    shift = int((10 + tmax) * out.info['sfreq'])
    events_offset = _update_offset(None, out.events, shift)
    for ii, epochs in enumerate(epochs_list[1:], 1):
        _compare_epochs_infos(epochs.info, info, ii)
        if not np.allclose(epochs.times, epochs_list[0].times):
            raise ValueError('Epochs must have same times')

        if epochs.baseline != baseline:
            raise ValueError('Baseline must be same for all epochs')

        # compare event_id
        common_keys = list(set(event_id).intersection(set(epochs.event_id)))
        for key in common_keys:
            if not event_id[key] == epochs.event_id[key]:
                msg = ('event_id values must be the same for identical keys '
                       'for all concatenated epochs. Key "{}" maps to {} in '
                       'some epochs and to {} in others.')
                raise ValueError(msg.format(key, event_id[key],
                                            epochs.event_id[key]))

        if with_data:
            epochs.drop_bad()
            offsets.append(len(epochs))
        evs = epochs.events.copy()
        # add offset
        if add_offset:
            evs[:, 0] += events_offset
        # Update offset for the next iteration.
        events_offset = _update_offset(events_offset, epochs.events, shift)
        events.append(evs)
        selection = np.concatenate((selection, epochs.selection))
        drop_log = drop_log + epochs.drop_log
        event_id.update(epochs.event_id)
        metadata.append(epochs.metadata)
    events = np.concatenate(events, axis=0)
    # check to see if we exceeded our maximum event offset
    if events_offset == 0:
        events[:, 0] = np.arange(1, len(events) + 1)

    # Create metadata object (or make it None)
    n_have = sum(this_meta is not None for this_meta in metadata)
    if n_have == 0:
        metadata = None
    elif n_have != len(metadata):
        raise ValueError('%d of %d epochs instances have metadata, either '
                         'all or none must have metadata'
                         % (n_have, len(metadata)))
    else:
        pd = _check_pandas_installed(strict=False)
        if pd is not False:
            metadata = pd.concat(metadata)
        else:  # dict of dicts
            metadata = sum(metadata, list())
    assert len(offsets) == (len(epochs_list) if with_data else 0) + 1
    data = None
    if with_data:
        offsets = np.cumsum(offsets)
        for start, stop, epochs in zip(offsets[:-1], offsets[1:], epochs_list):
            this_data = epochs.get_data()
            if data is None:
                data = np.empty(
                    (offsets[-1], len(out.ch_names), len(out.times)),
                    dtype=this_data.dtype)
            data[start:stop] = this_data
    return (info, data, events, event_id, tmin, tmax, metadata, baseline,
            selection, drop_log, verbose)


def _finish_concat(info, data, events, event_id, tmin, tmax, metadata,
                   baseline, selection, drop_log, verbose):
    """Finish concatenation for epochs not read from disk."""
    selection = np.where([len(d) == 0 for d in drop_log])[0]
    out = BaseEpochs(
        info, data, events, event_id, tmin, tmax, baseline=baseline,
        selection=selection, drop_log=drop_log, proj=False,
        on_missing='ignore', metadata=metadata, verbose=verbose)
    out.drop_bad()
    return out


def concatenate_epochs(epochs_list, add_offset=True):
    """Concatenate a list of epochs into one epochs object.

    Parameters
    ----------
    epochs_list : list
        List of Epochs instances to concatenate (in order).
    add_offset : bool
        If True, a fixed offset is added to the event times from different
        Epochs sets, such that they are easy to distinguish after the
        concatenation.
        If False, the event times are unaltered during the concatenation.

    Returns
    -------
    epochs : instance of Epochs
        The result of the concatenation (first Epochs instance passed in).

    Notes
    -----
    .. versionadded:: 0.9.0
    """
    return _finish_concat(*_concatenate_epochs(epochs_list,
                                               add_offset=add_offset))


@verbose
def average_movements(epochs, head_pos=None, orig_sfreq=None, picks=None,
                      origin='auto', weight_all=True, int_order=8, ext_order=3,
                      destination=None, ignore_ref=False, return_mapping=False,
                      mag_scale=100., verbose=None):
    """Average data using Maxwell filtering, transforming using head positions.

    Parameters
    ----------
    epochs : instance of Epochs
        The epochs to operate on.
    %(maxwell_pos)s
    orig_sfreq : float | None
        The original sample frequency of the data (that matches the
        event sample numbers in ``epochs.events``). Can be ``None``
        if data have not been decimated or resampled.
    %(picks_all_data)s
    %(maxwell_origin)s
    weight_all : bool
        If True, all channels are weighted by the SSS basis weights.
        If False, only MEG channels are weighted, other channels
        receive uniform weight per epoch.
    %(maxwell_int)s
    %(maxwell_ext)s
    %(maxwell_dest)s
    %(maxwell_ref)s
    return_mapping : bool
        If True, return the mapping matrix.
    %(maxwell_mag)s

        .. versionadded:: 0.13
    %(verbose)s

    Returns
    -------
    evoked : instance of Evoked
        The averaged epochs.

    See Also
    --------
    mne.preprocessing.maxwell_filter
    mne.chpi.read_head_pos

    Notes
    -----
    The Maxwell filtering version of this algorithm is described in [1]_,
    in section V.B "Virtual signals and movement correction", equations
    40-44. For additional validation, see [2]_.

    Regularization has not been added because in testing it appears to
    decrease dipole localization accuracy relative to using all components.
    Fine calibration and cross-talk cancellation, however, could be added
    to this algorithm based on user demand.

    .. versionadded:: 0.11

    References
    ----------
    .. [1] Taulu S. and Kajola M. "Presentation of electromagnetic
           multichannel data: The signal space separation method,"
           Journal of Applied Physics, vol. 97, pp. 124905 1-10, 2005.
    .. [2] Wehner DT, Hämäläinen MS, Mody M, Ahlfors SP. "Head movements
           of children in MEG: Quantification, effects on source
           estimation, and compensation. NeuroImage 40:541–550, 2008.
    """  # noqa: E501
    from .preprocessing.maxwell import (_trans_sss_basis, _reset_meg_bads,
                                        _check_usable, _col_norm_pinv,
                                        _get_n_moments, _get_mf_picks_fix_mags,
                                        _prep_mf_coils, _check_destination,
                                        _remove_meg_projs, _get_coil_scale)
    if head_pos is None:
        raise TypeError('head_pos must be provided and cannot be None')
    from .chpi import head_pos_to_trans_rot_t
    if not isinstance(epochs, BaseEpochs):
        raise TypeError('epochs must be an instance of Epochs, not %s'
                        % (type(epochs),))
    orig_sfreq = epochs.info['sfreq'] if orig_sfreq is None else orig_sfreq
    orig_sfreq = float(orig_sfreq)
    if isinstance(head_pos, np.ndarray):
        head_pos = head_pos_to_trans_rot_t(head_pos)
    trn, rot, t = head_pos
    del head_pos
    _check_usable(epochs)
    origin = _check_origin(origin, epochs.info, 'head')
    recon_trans = _check_destination(destination, epochs.info, True)

    logger.info('Aligning and averaging up to %s epochs'
                % (len(epochs.events)))
    if not np.array_equal(epochs.events[:, 0], np.unique(epochs.events[:, 0])):
        raise RuntimeError('Epochs must have monotonically increasing events')
    info_to = epochs.info.copy()
    meg_picks, mag_picks, grad_picks, good_mask, _ = \
        _get_mf_picks_fix_mags(info_to, int_order, ext_order, ignore_ref)
    coil_scale, mag_scale = _get_coil_scale(
        meg_picks, mag_picks, grad_picks, mag_scale, info_to)
    n_channels, n_times = len(epochs.ch_names), len(epochs.times)
    other_picks = np.setdiff1d(np.arange(n_channels), meg_picks)
    data = np.zeros((n_channels, n_times))
    count = 0
    # keep only MEG w/bad channels marked in "info_from"
    info_from = pick_info(info_to, meg_picks[good_mask], copy=True)
    all_coils_recon = _prep_mf_coils(info_to, ignore_ref=ignore_ref)
    all_coils = _prep_mf_coils(info_from, ignore_ref=ignore_ref)
    # remove MEG bads in "to" info
    _reset_meg_bads(info_to)
    # set up variables
    w_sum = 0.
    n_in, n_out = _get_n_moments([int_order, ext_order])
    S_decomp = 0.  # this will end up being a weighted average
    last_trans = None
    decomp_coil_scale = coil_scale[good_mask]
    exp = dict(int_order=int_order, ext_order=ext_order, head_frame=True,
               origin=origin)
    n_in = _get_n_moments(int_order)
    for ei, epoch in enumerate(epochs):
        event_time = epochs.events[epochs._current - 1, 0] / orig_sfreq
        use_idx = np.where(t <= event_time)[0]
        if len(use_idx) == 0:
            trans = info_to['dev_head_t']['trans']
        else:
            use_idx = use_idx[-1]
            trans = np.vstack([np.hstack([rot[use_idx], trn[[use_idx]].T]),
                               [[0., 0., 0., 1.]]])
        loc_str = ', '.join('%0.1f' % tr for tr in (trans[:3, 3] * 1000))
        if last_trans is None or not np.allclose(last_trans, trans):
            logger.info('    Processing epoch %s (device location: %s mm)'
                        % (ei + 1, loc_str))
            reuse = False
            last_trans = trans
        else:
            logger.info('    Processing epoch %s (device location: same)'
                        % (ei + 1,))
            reuse = True
        epoch = epoch.copy()  # because we operate inplace
        if not reuse:
            S = _trans_sss_basis(exp, all_coils, trans,
                                 coil_scale=decomp_coil_scale)
            # Get the weight from the un-regularized version (eq. 44)
            weight = np.linalg.norm(S[:, :n_in])
            # XXX Eventually we could do cross-talk and fine-cal here
            S *= weight
        S_decomp += S  # eq. 41
        epoch[slice(None) if weight_all else meg_picks] *= weight
        data += epoch  # eq. 42
        w_sum += weight
        count += 1
    del info_from
    mapping = None
    if count == 0:
        data.fill(np.nan)
    else:
        data[meg_picks] /= w_sum
        data[other_picks] /= w_sum if weight_all else count
        # Finalize weighted average decomp matrix
        S_decomp /= w_sum
        # Get recon matrix
        # (We would need to include external here for regularization to work)
        exp['ext_order'] = 0
        S_recon = _trans_sss_basis(exp, all_coils_recon, recon_trans)
        exp['ext_order'] = ext_order
        # We could determine regularization on basis of destination basis
        # matrix, restricted to good channels, as regularizing individual
        # matrices within the loop above does not seem to work. But in
        # testing this seemed to decrease localization quality in most cases,
        # so we do not provide the option here.
        S_recon /= coil_scale
        # Invert
        pS_ave = _col_norm_pinv(S_decomp)[0][:n_in]
        pS_ave *= decomp_coil_scale.T
        # Get mapping matrix
        mapping = np.dot(S_recon, pS_ave)
        # Apply mapping
        data[meg_picks] = np.dot(mapping, data[meg_picks[good_mask]])
    info_to['dev_head_t'] = recon_trans  # set the reconstruction transform
    evoked = epochs._evoked_from_epoch_data(data, info_to, picks,
                                            n_events=count, kind='average',
                                            comment=epochs._name)
    _remove_meg_projs(evoked)  # remove MEG projectors, they won't apply now
    logger.info('Created Evoked dataset from %s epochs' % (count,))
    return (evoked, mapping) if return_mapping else evoked


@verbose
def make_fixed_length_epochs(raw, duration=1., preload=False,
                             reject_by_annotation=True, proj=True, overlap=0.,
                             verbose=None):
    """Divide continuous raw data into equal-sized consecutive epochs.

    Parameters
    ----------
    raw : instance of Raw
        Raw data to divide into segments.
    duration : float
        Duration of each epoch in seconds. Defaults to 1.
    %(preload)s
    %(reject_by_annotation_epochs)s

        .. versionadded:: 0.21.0
    %(proj_epochs)s

        .. versionadded:: 0.22.0
    overlap : float
        The overlap between epochs, in seconds. Must be
        ``0 <= overlap < duration``. Default is 0, i.e., no overlap.

        .. versionadded:: 0.23.0
    %(verbose)s

    Returns
    -------
    epochs : instance of Epochs
        Segmented data.

    Notes
    -----
    .. versionadded:: 0.20
    """
    events = make_fixed_length_events(raw, 1, duration=duration,
                                      overlap=overlap)
    delta = 1. / raw.info['sfreq']
    return Epochs(raw, events, event_id=[1], tmin=0, tmax=duration - delta,
                  baseline=None, preload=preload,
                  reject_by_annotation=reject_by_annotation, proj=proj,
                  verbose=verbose)
