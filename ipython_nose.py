import cgi
import os
import traceback
import re
import shlex
import string
import types
import uuid
import json

from nose import core as nose_core
from nose import loader as nose_loader
from nose.config import Config, all_config_files
from nose.plugins.base import Plugin
from nose.plugins.skip import SkipTest
from nose.plugins.manager import DefaultPluginManager
from nose.selector import defaultSelector
from IPython.core import magic
from IPython.display import display


class Template(string.Formatter):
    def __init__(self, template):
        self._template = template

    def format(self, **context):
        return self.vformat(self._template, (), context)

    def convert_field(self, value, conversion):
        if conversion == 'e':
            return cgi.escape(value)
        else:
            return super(Template, self).convert_field(value, conversion)


class DummyUnittestStream:
    def write(self, *arg):
        pass

    def writeln(self, *arg):
        pass

    def flush(self, *arg):
        pass


class NotebookLiveOutput(object):
    def __init__(self):
        self.output_id = 'ipython_nose_%s' % uuid.uuid4().hex

    def finalize(self, pass_or_fail, n_tests, n_failures, n_errors, tests):
        # tell frontend whether tests passed or failed
        out = {
               "success": pass_or_fail,
               "summary": {
                   "tests":    n_tests,      # the total number of tests,
                   "failures": n_failures,   # the number of tests that failed
                   "errors":   n_errors      # The number of tests that errored out, which mean it couldn't run but dind'
                   },
               "tests": list(map(self._dump_test, tests))
               }

        output = {'application/json': json.dumps(out)}
        display(output, raw = True)
        return output
        
    def write_chars(self, chars):
        pass

    def write_line(self, line):
        pass

    def _dump_test(self, test_tuple):
        test, exc, res  = test_tuple
        return { "name": str(test.test.test.__name__), # NoseTest.SomeTestCaseClass.func
                 "success": res == "success",
                 "message": ''.join(traceback.format_exception(*exc)[-1:]) if res != "success" else ""}


def html_escape(s):
    return cgi.escape(str(s))


class IPythonDisplay(Plugin):
    """Do something nice in IPython."""

    name = 'ipython-html'
    enabled = True
    score = 2

    def __init__(self, verbose=False, expand_tracebacks=False):
        super(IPythonDisplay, self).__init__()
        self.verbose = verbose
        self.expand_tracebacks = expand_tracebacks
        self.html = []
        self.num_tests = 0
        self.failures = []
        self.tests = []
        self.skipped = 0
        self.json_output = ""

    _summary_template_text = Template('''{text}\n''')

    def _summary(self, numtests, numfailed, numskipped, template):
        text = "%d/%d tests passed" % (numtests - numfailed, numtests)
        if numfailed > 0:
            text += "; %d failed" % numfailed
        if numskipped > 0:
            text += "; %d skipped" % numskipped

        failpercent = int(float(numfailed) / numtests * 100)
        if numfailed > 0 and failpercent < 5:
            # Ensure the red bar is visible
            failpercent = 5

        skippercent = int(float(numskipped) / numtests * 100)
        if numskipped > 0 and skippercent < 5:
            # Ditto for the yellow bar
            skippercent = 5

        passpercent = 100 - failpercent - skippercent

        return template.format(
            text=text, failpercent=failpercent, skippercent=skippercent,
            passpercent=passpercent)

    _tracebacks_template_text = Template(
        '''========\n{name}\n========\n{formatted_traceback}\n''')

    def _tracebacks(self, failures, template):
        output = []
        for test, exc, res in failures:
            name = test.shortDescription() or str(test)
            formatted_traceback = ''.join(traceback.format_exception(*exc))
            output.append(template.format(
                name=name, formatted_traceback=formatted_traceback,
                hide_traceback_style=('block' if self.expand_tracebacks
                                      else 'none')
            ))
        return ''.join(output)

    def _write_test_line(self, test, status):
        self.live_output.write_line(
            "{} ... {}".format(test.shortDescription() or str(test), status))

    def addSuccess(self, test):
        if self.verbose:
            self._write_test_line(test, 'pass')
        else:
            self.live_output.write_chars('.')
        self.tests.append((test, None, 'success'))

    def addError(self, test, err):
        if issubclass(err[0], SkipTest):
            return self.addSkip(test)
        if self.verbose:
            self._write_test_line(test, 'error')
        else:
            self.live_output.write_chars('E')
        self.failures.append((test, err, 'error'))
        self.tests.append((test, err, 'error'))

    def addFailure(self, test, err):
        if self.verbose:
            self._write_test_line(test, 'fail')
        else:
            self.live_output.write_chars('F')
        self.failures.append((test, err, 'failure'))
        self.tests.append((test, err, 'failure'))

    # Deprecated in newer versions of nose; skipped tests are handled in
    # addError in newer versions
    def addSkip(self, test):
        if self.verbose:
            self.live_output.write_line(str(test) + " ... SKIP")
        else:
            self.live_output.write_chars('S')
        self.skipped += 1

    def begin(self):
        self.live_output = NotebookLiveOutput()

    def finalize(self, result):
        self.result = result
        self.json_output = self.live_output.finalize(
                result.wasSuccessful(),
                self.num_tests, self.n_failures, self.n_errors,
                self.tests)

    def setOutputStream(self, stream):
        # grab for own use
        self.stream = stream
        return DummyUnittestStream()

    def startContext(self, ctx):
        pass

    def stopContext(self, ctx):
        pass

    def startTest(self, test):
        self.num_tests += 1

    def stopTest(self, test):
        pass

    @property
    def n_failures(self):
        return len([entry for entry in self.failures if entry[2] == 'failure'])

    @property
    def n_errors(self):
        return len([entry for entry in self.failures if entry[2] == 'error'])

    def _repr_pretty_(self, p, cycle):
        if self.num_tests <= 0:
            p.text('No tests found.')
            return
        p.text(self._summarize())
        p.text(self._summarize_tracebacks())

    def _summarize(self):
        return self._summary(self.num_tests, len(self.failures), 
                             self.skipped, self._summary_template_text)

    def _summarize_tracebacks(self):
        return self._tracebacks(self.failures, self._tracebacks_template_text)


def get_ipython_user_ns_as_a_module():
    test_module = types.ModuleType('test_module')
    test_module.__dict__.update(get_ipython().user_ns)
    return test_module


def makeNoseConfig(env):
    """Load a Config, pre-filled with user config files if any are
    found.
    """
    cfg_files = all_config_files()
    manager = DefaultPluginManager()
    return Config(env=env, files=cfg_files, plugins=manager)


class ExcludingTestSelector(defaultSelector):
    def __init__(self, config, excluded_objects):
        super(ExcludingTestSelector, self).__init__(config)
        self.excluded_objects = list(excluded_objects)

    def _in_excluded_objects(self, obj):
        for excluded_object in self.excluded_objects:
            try:
                if obj == excluded_object:
                    return True
            except Exception:
                return False
        return False

    def wantClass(self, cls):
        if self._in_excluded_objects(cls):
            return False
        else:
            return super(ExcludingTestSelector, self).wantClass(cls)

    def wantFunction(self, function):
        if self._in_excluded_objects(function):
            return False
        else:
            return super(ExcludingTestSelector, self).wantFunction(function)

    def wantMethod(self, method):
        if self._in_excluded_objects(type(method.__self__)):
            return False
        else:
            return super(ExcludingTestSelector, self).wantMethod(method)


def nose(line, cell=None, test_module=get_ipython_user_ns_as_a_module):
    if callable(test_module):
        test_module = test_module()
    config = makeNoseConfig(os.environ)
    if cell is None:
        # Called as the %nose line magic.
        # All objects in the notebook namespace should be considered for the
        # test suite.
        selector = None
    else:
        # Called as the %%nose cell magic.
        # Classes and functions defined outside the cell should be excluded from
        # the test run.
        selector = ExcludingTestSelector(config, test_module.__dict__.values())
        # Evaluate the cell and add objects it defined into the test module.
        exec(cell, test_module.__dict__)
    loader = nose_loader.TestLoader(config=config, selector=selector)
    tests = loader.loadTestsFromModule(test_module)
    extra_args = shlex.split(str(line))
    expand_tracebacks = '--expand-tracebacks' in extra_args
    if expand_tracebacks:
        extra_args.remove('--expand-tracebacks')
    argv = ['ipython-nose', '--with-ipython-html', '--no-skip'] + extra_args
    verbose = '-v' in extra_args
    plug = IPythonDisplay(verbose=verbose, expand_tracebacks=expand_tracebacks)

    nose_core.TestProgram(
        argv=argv, suite=tests, addplugins=[plug], exit=False, config=config)

    ## for debugging
    #get_ipython().user_ns['nose_obj'] = plug
    return plug


def load_ipython_extension(ipython):
    magic.register_line_cell_magic(nose)
