# mypy: ignore-errors

import collections
import json
from typing import List

from pytorch_pfn_extras import reporting
from pytorch_pfn_extras.training import extension
from pytorch_pfn_extras.training import trigger as trigger_module

try:
    import pandas

    _pandas_available = True
except ImportError:
    _pandas_available = False


class LogWriterSaveFunc:

    def __init__(self, format, append):
        self._format = format
        self._append = append

    def __call__(self, target, file_o):
        if self._format == 'json':
            if self._append:
                raise ValueError(
                    'LogReport does not support json format with append mode.')
            log = json.dumps(target, indent=4)
        elif self._format == 'json-lines':
            # Add a new line at the end for subsequent appends
            log = '\n'.join([json.dumps(x) for x in target]) + '\n'
        elif self._format == 'yaml':
            import yaml

            # This is to dump ordered dicts as regular dicts
            def dict_representer(dumper, data):
                return dumper.represent_dict(data.items())
            yaml.add_representer(collections.OrderedDict, dict_representer)
            # yaml.add_constructor(_mapping_tag, dict_constructor)
            log = yaml.dump(target)
        else:
            raise ValueError('Unknown format: {}'.format(self._format))
        file_o.write(bytes(log.encode('ascii')))


class _LogBuffer:

    def __init__(self) -> None:
        self.lookers = {}
        self._log = []
        self._offset = 0

    def _normalize(self) -> None:
        min_looker_index = min(self.lookers.values())
        if min_looker_index > self._offset:
            self._log = self._log[min_looker_index - self._offset:]
            self._offset = min_looker_index

    def append(self, observation) -> None:
        self._log.append(observation)

    def _get(self, looker_id: int) -> List[str]:
        return self._log[self.lookers[looker_id] - self._offset:]

    def _clear(self, looker_id: int) -> None:
        if looker_id not in self.lookers:
            raise ValueError(f'looker {looker_id} is not registered')
        self.lookers[looker_id] = len(self._log) + self._offset
        self._normalize()

    def emit_new_looker(self) -> '_LogLooker':
        looker_id = len(self.lookers)
        assert looker_id not in self.lookers
        self.lookers[looker_id] = len(self._log) + self._offset
        return _LogLooker(self, looker_id)

    def state_dict(self) -> List[str]:
        return json.dumps(self._log)

    def load_state_dict(self, to_load: List[str]) -> None:
        self._log = json.loads(to_load)


class _LogLooker:

    def __init__(self, log_buffer: _LogBuffer, looker_id: int) -> None:
        self._log_buffer = log_buffer
        self._looker_id = looker_id

    def get(self) -> List[str]:
        return self._log_buffer._get(self._looker_id)

    def clear(self) -> None:
        return self._log_buffer._clear(self._looker_id)


class LogReport(extension.Extension):

    """
    An extension to output the accumulated results to a log file.

    This extension accumulates the observations of the manager to
    :class:`~pytorch_pfn_extras.DictSummary` at a regular interval specified
    by a supplied trigger, and writes them into a log file in JSON format.

    There are two triggers to handle this extension. One is the trigger to
    invoke this extension, which is used to handle the timing of accumulating
    the results. It is set to ``1, 'iteration'`` by default. The other is the
    trigger to determine when to emit the result. When this trigger returns
    True, this extension appends the summary of accumulated values to the list
    of past summaries, and writes the list to the log file. Then, this
    extension makes a new fresh summary object which is used until the next
    time that the trigger fires.

    It also adds some entries to each result dictionary.

    - ``'epoch'`` and ``'iteration'`` are the epoch and iteration counts at the
      output, respectively.
    - ``'elapsed_time'`` is the elapsed time in seconds since the training
      begins. The value is taken from :attr:`ExtensionsManager.elapsed_time`.

    Args:
        keys (iterable of strs): Keys of values to accumulate. If this is None,
            all the values are accumulated and output to the log file.
        trigger: Trigger that decides when to aggregate the result and output
            the values. This is distinct from the trigger of this extension
            itself. If it is a tuple in the form ``<int>, 'epoch'`` or
            ``<int>, 'iteration'``, it is passed to :class:`IntervalTrigger`.
        postprocess: Callback to postprocess the result dictionaries. Each
            result dictionary is passed to this callback on the output. This
            callback can modify the result dictionaries, which are used to
            output to the log file.
        filename (str): Name of the log file under the output directory. It can
            be a format string: the last result dictionary is passed for the
            formatting. For example, users can use '{iteration}' to separate
            the log files for different iterations. If the log name is None, it
            does not output the log to any file.
            For historical reasons ``log_name`` is also accepted as an alias
            of this argument.
        append (bool, optional): If the file is JSON Lines or YAML, contents
            will be appended instead of rewritting the file every call.
        format (str, optional): accepted values are `'json'`, `'json-lines'`
            and `'yaml'`.
        writer (writer object, optional): must be callable.
            object to dump the log to. If specified, it needs to have a correct
            `savefun` defined. The writer can override the save location in
            the :class:`pytorch_pfn_extras.training.ExtensionsManager` object

    """

    def __init__(self, keys=None, trigger=(1, 'epoch'), postprocess=None,
                 filename=None, append=False, format=None, **kwargs):
        self._keys = keys
        self._trigger = trigger_module.get_trigger(trigger)
        self._postprocess = postprocess
        self._log_buffer = _LogBuffer()
        self._log_looker = self._log_buffer.emit_new_looker()
        # When using a writer, it needs to have a savefun defined
        # to deal with a string.
        self._writer = kwargs.get('writer', None)

        log_name = kwargs.get('log_name', 'log')
        if filename is None:
            filename = log_name
        del log_name  # avoid accidental use
        self._log_name = filename

        if format is None and filename is not None:
            if filename.endswith('.jsonl'):
                format = 'json-lines'
            elif filename.endswith('.yaml'):
                format = 'yaml'
            else:
                format = 'json'

        self._append = append
        self._format = format
        self._init_summary()

    def __call__(self, manager):
        # accumulate the observations
        keys = self._keys
        observation = manager.observation
        summary = self._summary

        if keys is None:
            summary.add(observation)
        else:
            summary.add({k: observation[k] for k in keys if k in observation})

        writer = manager.writer if self._writer is None else self._writer

        if manager.is_before_training or self._trigger(manager):
            # output the result
            stats = self._summary.compute_mean()
            stats_cpu = {}
            for name, value in stats.items():
                stats_cpu[name] = float(value)  # copy to CPU

            stats_cpu['epoch'] = manager.epoch
            stats_cpu['iteration'] = manager.iteration
            stats_cpu['elapsed_time'] = manager.elapsed_time

            if self._postprocess is not None:
                self._postprocess(stats_cpu)

            if self._append:
                self._log_looker.clear()
            self._log_buffer.append(stats_cpu)

            # write to the log file
            if self._log_name is not None:
                log_name = self._log_name.format(**stats_cpu)
                out = manager.out
                savefun = LogWriterSaveFunc(self._format, self._append)
                writer(log_name, out, self._log_looker.get(),
                       savefun=savefun, append=self._append)

            # reset the summary for the next output
            self._init_summary()

    @property
    def log(self):
        """The current list of observation dictionaries."""
        return self._log_looker.get()

    def state_dict(self):
        state = {}
        if hasattr(self._trigger, 'state_dict'):
            state['_trigger'] = self._trigger.state_dict()

        try:
            state['_summary'] = self._summary.state_dict()
        except KeyError:
            pass
        state['_log'] = self._log_buffer.state_dict()
        return state

    def load_state_dict(self, to_load):
        if hasattr(self._trigger, 'load_state_dict'):
            self._trigger.load_state_dict(to_load['_trigger'])
        self._summary.load_state_dict(to_load['_summary'])
        self._log_buffer.load_state_dict(to_load['_log'])

    def _init_summary(self):
        self._summary = reporting.DictSummary()

    def to_dataframe(self):
        if not _pandas_available:
            raise ImportError(
                "Need to install pandas to use `to_dataframe` method."
            )
        return pandas.DataFrame(self._log_looker.get())
