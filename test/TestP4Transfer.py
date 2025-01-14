# -*- encoding: UTF8 -*-
# Tests for the P4Transfer.py module.

from __future__ import print_function

import sys
import time
import P4
import subprocess
import inspect
import platform
from textwrap import dedent
import unittest
import os
import shutil
import stat
import re
import glob
import argparse
import datetime
from ruamel.yaml import YAML

# Bring in module to be tested
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import logutils         # noqa: E402
import P4Transfer       # noqa: E402

yaml = YAML()

python3 = sys.version_info[0] >= 3
if sys.hexversion < 0x02070000 or (0x0300000 < sys.hexversion < 0x0303000):
    sys.exit("Python 2.7 or 3.3 or newer is required to run this program.")

if python3:
    from io import StringIO
else:
    from StringIO import StringIO

P4D = "p4d"     # This can be overridden via command line stuff
P4USER = "testuser"
P4CLIENT = "test_ws"
TEST_ROOT = '_testrun_transfer'
TRANSFER_CLIENT = "transfer"
TRANSFER_CONFIG = "transfer.yaml"

TEST_COUNTER_NAME = "P4Transfer"
INTEG_ENGINE = 3

saved_stdoutput = StringIO()
test_logger = None


def onRmTreeError(function, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    os.remove(path)


def ensureDirectory(directory):
    if not os.path.isdir(directory):
        os.makedirs(directory)


def localDirectory(root, *dirs):
    "Create and ensure it exists"
    dir_path = os.path.join(root, *dirs)
    ensureDirectory(dir_path)
    return dir_path


def create_file(file_name, contents):
    "Create file with specified contents"
    ensureDirectory(os.path.dirname(file_name))
    if python3:
        contents = bytes(contents.encode())
    with open(file_name, 'wb') as f:
        f.write(contents)


def append_to_file(file_name, contents):
    "Append contents to file"
    if python3:
        contents = bytes(contents.encode())
    with open(file_name, 'ab+') as f:
        f.write(contents)


def getP4ConfigFilename():
    "Returns os specific filename"
    if 'P4CONFIG' in os.environ:
        return os.environ['P4CONFIG']
    if os.name == "nt":
        return "p4config.txt"
    return ".p4config"


class P4Server:
    def __init__(self, root, logger, caseInsensitive=False):
        self.root = root
        self.logger = logger
        self.server_root = os.path.join(root, "server")
        self.client_root = os.path.join(root, "client")

        ensureDirectory(self.root)
        ensureDirectory(self.server_root)
        ensureDirectory(self.client_root)

        caseFlag = ""
        if caseInsensitive:
            caseFlag = "-C1 "
        self.p4d = P4D
        self.port = "rsh:%s -r \"%s\" -L log %s -i" % (self.p4d, self.server_root, caseFlag)
        self.p4 = P4.P4()
        self.p4.port = self.port
        self.p4.user = P4USER
        self.p4.client = P4CLIENT
        self.p4.connect()

        self.p4cmd('depots')  # triggers creation of the user
        self.p4cmd('configure', 'set', 'dm.integ.engine=%d' % INTEG_ENGINE)

        self.p4.disconnect()  # required to pick up the configure changes
        self.p4.connect()

        self.client_name = P4CLIENT
        client = self.p4.fetch_client(self.client_name)
        client._root = self.client_root
        client._lineend = 'unix'
        self.p4.save_client(client)

    def shutDown(self):
        if self.p4.connected():
            self.p4.disconnect()

    def createTransferClient(self, name, root):
        pass

    def run_cmd(self, cmd, dir=".", get_output=True, timeout=35, stop_on_error=True):
        "Run cmd logging input and output"
        output = ""
        try:
            self.logger.debug("Running: %s" % cmd)
            if get_output:
                p = subprocess.Popen(cmd, cwd=dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, shell=True)
                if python3:
                    output, _ = p.communicate(timeout=timeout)
                else:
                    output, _ = p.communicate()
                # rc = p.returncode
                self.logger.debug("Output:\n%s" % output)
            else:
                result = subprocess.check_call(cmd, stderr=subprocess.STDOUT, shell=True, timeout=timeout)
                self.logger.debug('Result: %d' % result)
        except subprocess.CalledProcessError as e:
            self.logger.debug("Output: %s" % e.output)
            if stop_on_error:
                msg = 'Failed run_cmd: %d %s' % (e.returncode, str(e))
                self.logger.debug(msg)
                raise e
        except Exception as e:
            self.logger.debug("Output: %s" % output)
            if stop_on_error:
                msg = 'Failed run_cmd: %s' % str(e)
                self.logger.debug(msg)
                raise e
        return output

    def enableUnicode(self):
        cmd = '%s -r "%s" -L log -vserver=3 -xi' % (self.p4d, self.server_root)
        output = self.run_cmd(cmd, dir=self.server_root, get_output=True, stop_on_error=True)
        self.logger.debug(output)

    def getCounter(self):
        "Returns value of counter as integer"
        result = self.p4.run('counter', TEST_COUNTER_NAME)
        if result and 'counter' in result[0]:
            return int(result[0]['value'])
        return 0

    def p4cmd(self, *args):
        "Execute p4 cmd while logging arguments and results"
        if not self.logger:
            self.logger = logutils.getLogger(P4Transfer.LOGGER_NAME)
        self.logger.debug('testp4:', args)
        output = self.p4.run(args)
        self.logger.debug('testp4r:', output)
        return output


class TestP4TransferBase(unittest.TestCase):
    """Base class for tests"""

    def __init__(self, methodName='runTest'):
        global saved_stdoutput, test_logger
        saved_stdoutput.truncate(0)
        if test_logger is None:
            test_logger = logutils.getLogger(P4Transfer.LOGGER_NAME, stream=saved_stdoutput)
        else:
            logutils.resetLogger(P4Transfer.LOGGER_NAME)
        self.logger = test_logger
        super(TestP4TransferBase, self).__init__(methodName=methodName)

    def assertRegex(self, *args, **kwargs):
        if python3:
            return super(TestP4TransferBase, self).assertRegex(*args, **kwargs)
        else:
            return super(TestP4TransferBase, self).assertRegexpMatches(*args, **kwargs)

    def assertContentsEqual(self, expected, content):
        if python3:
            content = content.decode()
        self.assertEqual(expected, content)

    def setUp(self):
        self.setDirectories()

    def tearDown(self):
        self.source.shutDown()
        self.target.shutDown()
        time.sleep(0.1)
        # self.cleanupTestTree()

    def setDirectories(self, caseInsensitive=False):
        self.startdir = os.getcwd()
        self.transfer_root = os.path.join(self.startdir, TEST_ROOT)
        self.cleanupTestTree()

        ensureDirectory(self.transfer_root)

        self.source = P4Server(os.path.join(self.transfer_root, 'source'), self.logger, caseInsensitive=caseInsensitive)
        self.target = P4Server(os.path.join(self.transfer_root, 'target'), self.logger, caseInsensitive=caseInsensitive)

        self.transfer_client_root = localDirectory(self.transfer_root, 'transfer_client')
        self.writeP4Config()

    def writeP4Config(self):
        "Write appropriate files - useful for occasional manual debugging"
        p4config_filename = getP4ConfigFilename()
        srcP4Config = os.path.join(self.transfer_root, 'source', p4config_filename)
        targP4Config = os.path.join(self.transfer_root, 'target', p4config_filename)
        transferP4Config = os.path.join(self.transfer_client_root, p4config_filename)
        with open(srcP4Config, "w") as fh:
            fh.write('P4PORT=%s\n' % self.source.port)
            fh.write('P4USER=%s\n' % self.source.p4.user)
            fh.write('P4CLIENT=%s\n' % self.source.p4.client)
        with open(targP4Config, "w") as fh:
            fh.write('P4PORT=%s\n' % self.target.port)
            fh.write('P4USER=%s\n' % self.target.p4.user)
            fh.write('P4CLIENT=%s\n' % self.target.p4.client)
        with open(transferP4Config, "w") as fh:
            fh.write('P4PORT=%s\n' % self.target.port)
            fh.write('P4USER=%s\n' % self.target.p4.user)
            fh.write('P4CLIENT=%s\n' % TRANSFER_CLIENT)

    def cleanupTestTree(self):
        os.chdir(self.startdir)
        if os.path.isdir(self.transfer_root):
            shutil.rmtree(self.transfer_root, False, onRmTreeError)

    def getDefaultOptions(self):
        config = {}
        config['transfer_client'] = TRANSFER_CLIENT
        config['workspace_root'] = self.transfer_client_root
        config['views'] = [{'src': '//depot/inside/...',
                            'targ': '//depot/import/...'}]
        return config

    def setupTransfer(self):
        """Creates a config file with default mappings"""
        msg = "Test: %s ======================" % inspect.stack()[1][3]
        self.logger.debug(msg)
        config = self.getDefaultOptions()
        self.createConfigFile(options=config)

    def createConfigFile(self, srcOptions=None, targOptions=None, options=None):
        "Creates config file with extras if appropriate"
        if options is None:
            options = {}
        if srcOptions is None:
            srcOptions = {}
        if targOptions is None:
            targOptions = {}

        config = {}
        config['source'] = {}
        config['source']['p4port'] = self.source.port
        config['source']['p4user'] = P4USER
        config['source']['p4client'] = TRANSFER_CLIENT
        for opt in srcOptions.keys():
            config['source'][opt] = srcOptions[opt]

        config['target'] = {}
        config['target']['p4port'] = self.target.port
        config['target']['p4user'] = P4USER
        config['target']['p4client'] = TRANSFER_CLIENT
        for opt in targOptions.keys():
            config['target'][opt] = targOptions[opt]

        config['logfile'] = os.path.join(self.transfer_root, 'temp', 'test.log')
        if not os.path.exists(os.path.join(self.transfer_root, 'temp')):
            os.mkdir(os.path.join(self.transfer_root, 'temp'))
        config['counter_name'] = TEST_COUNTER_NAME

        for opt in options.keys():
            config[opt] = options[opt]

        # write the config file
        self.transfer_cfg = os.path.join(self.transfer_root, TRANSFER_CONFIG)
        with open(self.transfer_cfg, 'w') as f:
            yaml.dump(config, f)

    def run_P4Transfer(self, *args):
        self.logger.debug("-----------------------------Starting P4Transfer")
        base_args = ['-c', self.transfer_cfg, '-s']
        if args:
            base_args.extend(args)
        pt = P4Transfer.P4Transfer(*base_args)
        result = pt.replicate()
        return result

    def setTargetCounter(self, value):
        "Set's the target counter to specified value"
        self.target.p4.run('counter', TEST_COUNTER_NAME, str(value))

    def assertCounters(self, sourceValue, targetValue):
        sourceCounter = self.target.getCounter()
        targetCounter = len(self.target.p4.run("changes"))
        self.assertEqual(sourceCounter, sourceValue, "Source counter is not {} but {}".format(sourceValue, sourceCounter))
        self.assertEqual(targetCounter, targetValue, "Target counter is not {} but {}".format(targetValue, targetCounter))

    def applyJournalPatch(self, jnl_rec):
        "Apply journal patch"
        jnl_fix = os.path.join(self.source.server_root, "jnl_fix")
        create_file(jnl_fix, jnl_rec)
        cmd = '%s -r "%s" -jr "%s"' % (self.source.p4d, self.source.server_root, jnl_fix)
        self.logger.debug("Cmd: %s" % cmd)
        subprocess.check_output(cmd, shell=True)

    def dumpDBFiles(self, tables):
        "Extract journal records"
        all_output = []
        for table in tables.split(","):
            cmd = '%s -r "%s" -jd - %s' % (self.source.p4d, self.source.server_root, table)
            self.logger.debug("Cmd: %s" % cmd)
            output = subprocess.check_output(cmd, shell=True, universal_newlines=True)
            self.logger.debug("Output: %s" % output)
            all_output.append(output)
        results = [r for r in "\n".join(all_output).split("\n") if re.search("^@pv@", r)]
        return results


class TestP4TransferCaseInsensitive(TestP4TransferBase):
    """Case insensitive version"""

    def __init__(self, methodName='runTest'):
        global saved_stdoutput, test_logger
        saved_stdoutput.truncate(0)
        if test_logger is None:
            test_logger = logutils.getLogger(P4Transfer.LOGGER_NAME, stream=saved_stdoutput)
        else:
            logutils.resetLogger(P4Transfer.LOGGER_NAME)
        self.logger = test_logger
        super(TestP4TransferCaseInsensitive, self).__init__(methodName=methodName)

    def setUp(self):
        self.setDirectories(caseInsensitive=True)

    def testWildcardCharsAndCaseInsensitive(self):
        "Test filenames containing Perforce wildcards when required to be case insensitive"
        self.setupTransfer()

        options = self.getDefaultOptions()
        options["case_sensitive"] = "False"
        self.createConfigFile(options=options)

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")
        inside_file1 = os.path.join(inside, "@inside_file1")
        inside_file2 = os.path.join(inside, "%inside_file2")
        inside_file3 = os.path.join(inside, "#inside_file3")
        inside_file4 = os.path.join(inside, "C#", "inside_file4")
        outside_file1 = os.path.join(outside, "%outside_file")

        inside_file1Fixed = inside_file1.replace("@", "%40")
        inside_file2Fixed = inside_file2.replace("%", "%25")
        inside_file3Fixed = inside_file3.replace("#", "%23")
        inside_file4Fixed = inside_file4.replace("#", "%23")
        outside_file1Fixed = outside_file1.replace("%", "%25")

        create_file(inside_file1, 'Test content')
        create_file(inside_file3, 'Test content')
        create_file(inside_file4, 'Test content')
        create_file(outside_file1, 'Test content')
        self.source.p4cmd('add', '-f', inside_file1)
        self.source.p4cmd('add', '-f', inside_file3)
        self.source.p4cmd('add', '-f', inside_file4)
        self.source.p4cmd('add', '-f', outside_file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('integrate', outside_file1Fixed, inside_file2Fixed)
        self.source.p4cmd('submit', '-d', 'files integrated')

        self.source.p4cmd('edit', inside_file1Fixed)
        self.source.p4cmd('edit', inside_file3Fixed)
        self.source.p4cmd('edit', inside_file4Fixed)
        append_to_file(inside_file1, 'Different stuff')
        append_to_file(inside_file3, 'Different stuff')
        append_to_file(inside_file4, 'Different stuff')
        self.source.p4cmd('submit', '-d', 'files modified')

        self.source.p4cmd('integrate', "//depot/inside/*", "//depot/inside/new/*")
        self.source.p4cmd('submit', '-d', 'files branched')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 7)
        self.assertEqual(files[0]['depotFile'], '//depot/import/%23inside_file3')
        self.assertEqual(files[1]['depotFile'], '//depot/import/%25inside_file2')
        self.assertEqual(files[2]['depotFile'], '//depot/import/%40inside_file1')
        self.assertEqual(files[3]['depotFile'], '//depot/import/C%23/inside_file4')


class TestP4Transfer(TestP4TransferBase):

    def __init__(self, methodName='runTest'):
        global saved_stdoutput, test_logger
        saved_stdoutput.truncate(0)
        if test_logger is None:
            test_logger = logutils.getLogger(P4Transfer.LOGGER_NAME, stream=saved_stdoutput)
        else:
            logutils.resetLogger(P4Transfer.LOGGER_NAME)
        self.logger = test_logger
        super(TestP4Transfer, self).__init__(methodName=methodName)

    def testArgParsing(self):
        "Basic argparsing for the module"
        self.setupTransfer()
        args = ['-c', self.transfer_cfg, '-s']
        pt = P4Transfer.P4Transfer(*args)
        self.assertEqual(pt.options.config, self.transfer_cfg)
        self.assertTrue(pt.options.stoponerror)
        args = ['-c', self.transfer_cfg]
        pt = P4Transfer.P4Transfer(*args)

    def testArgParsingErrors(self):
        "Basic argparsing for the module"
        self.setupTransfer()
        # args = ['-c', self.transfer_cfg, '--end-datetime', '2020/1/1']
        # try:
        #     self.assertTrue(False, "Failed to get expected exception")
        # except Exception as e:
        #     pass
        args = ['-c', self.transfer_cfg, '--end-datetime', '2020/1/1 13:01']
        pt = P4Transfer.P4Transfer(*args)
        self.assertEqual(pt.options.config, self.transfer_cfg)
        self.assertFalse(pt.options.stoponerror)
        self.assertEqual(datetime.datetime(2020, 1, 1, 13, 1), pt.options.end_datetime)
        self.assertTrue(pt.endDatetimeExceeded())
        args = ['-c', self.transfer_cfg, '--end-datetime', '2040/1/1 13:01']
        pt = P4Transfer.P4Transfer(*args)
        self.assertEqual(pt.options.config, self.transfer_cfg)
        self.assertFalse(pt.options.stoponerror)
        self.assertEqual(datetime.datetime(2040, 1, 1, 13, 1), pt.options.end_datetime)
        self.assertFalse(pt.endDatetimeExceeded())

    def testTimeOffset(self):
        "Basic timeoffset calculations"
        obj = P4Transfer.UTCTimeFromSource()
        tests = [
            # src        result (UTC - src)
            ["0000",     0],
            ["asfd",     0],
            ["-0100",    60],
            ["+0100",   -60],
            ["-0700",    420],
            ["+0700",   -420],
        ]
        for t in tests:
            obj.setup(None, t[0])
            self.assertEqual(obj.offsetMins(), t[1])

    def testMaximum(self):
        "Test  only max number of changes are transferred"
        self.setupTransfer()
        args = ['-c', self.transfer_cfg, '-m1']
        pt = P4Transfer.P4Transfer(*args)
        self.assertEqual(pt.options.config, self.transfer_cfg)

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        desc = 'inside_file1 added'
        self.source.p4cmd('submit', '-d', desc)
        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "New line")
        desc = 'file edited'
        self.source.p4cmd('submit', '-d', desc)

        pt.replicate()
        self.assertCounters(1, 1)

        pt.replicate()
        self.assertCounters(2, 2)

    def testKTextDigests(self):
        "Calculate filesizes and digests for files which might contain keywords"
        self.setupTransfer()
        filename = os.path.join(self.transfer_root, 'test_file')

        create_file(filename, "line1\n")
        fileSize, digest = P4Transfer.getKTextDigest(filename)
        self.assertEqual(fileSize, 6)
        self.assertEqual(digest, "1ddab9058a07abc0db2605ab02a61a00")

        create_file(filename, "line1\nline2\n")
        fileSize, digest = P4Transfer.getKTextDigest(filename)
        self.assertEqual(fileSize, 12)
        self.assertEqual(digest, "4fcc82a88ee38e0aa16c17f512c685c9")

        create_file(filename, "line1\nsome $Id: //depot/fred.txt#2 $\nline2\n")
        fileSize, digest = P4Transfer.getKTextDigest(filename)
        self.assertEqual(fileSize, 10)
        self.assertEqual(digest, "ce8bc0316bdd8ad1f716f48e5c968854")

        create_file(filename, "line1\nsome $Id: //depot/fred.txt#2 $\nanother $Date: somedate$\nline2\n")
        fileSize, digest = P4Transfer.getKTextDigest(filename)
        self.assertEqual(fileSize, 10)
        self.assertEqual(digest, "ce8bc0316bdd8ad1f716f48e5c968854")

        create_file(filename, dedent("""\
        line1
        some $Id: //depot/fred.txt#2 $
        another $Date: somedate$
        line2
        """))
        fileSize, digest = P4Transfer.getKTextDigest(filename)
        self.assertEqual(fileSize, 10)
        self.assertEqual(digest, "ce8bc0316bdd8ad1f716f48e5c968854")

        create_file(filename, dedent("""\
        line1
        some $Id: //depot/fred.txt#2 $
        another $Date: somedata $
        another $DateTime: somedata $
        another $DateTime: somedata $
        $Change: 1234 $
        var = "$File: //depot/some/file.txt $";
        var = "$Revision: 45 $";
        var = "$Author: fred $";
        line2
        """))
        fileSize, digest = P4Transfer.getKTextDigest(filename)
        self.assertEqual(fileSize, 10)
        self.assertEqual(digest, "ce8bc0316bdd8ad1f716f48e5c968854")

    def testConfigValidation(self):
        "Make sure specified config options such as client views are valid"
        msg = "Test: %s ======================" % inspect.stack()[0][3]
        self.logger.debug(msg)

        self.createConfigFile()

        msg = ""
        try:
            base_args = ['-c', self.transfer_cfg, '-s']
            pt = P4Transfer.P4Transfer(*base_args)
            pt.setupReplicate()
        except Exception as e:
            msg = str(e)
        self.assertRegex(msg, "One of options views/stream_views must be specified")
        self.assertRegex(msg, "Option workspace_root must not be blank")

        # Integer config related options
        config = self.getDefaultOptions()
        config['poll_interval'] = 5
        config['report_interval'] = "10 * 5"
        self.createConfigFile(options=config)

        base_args = ['-c', self.transfer_cfg, '-s']
        pt = P4Transfer.P4Transfer(*base_args)
        pt.setupReplicate()
        self.assertEqual(5, pt.options.poll_interval)
        self.assertEqual(50, pt.options.report_interval)

        # Other streams related validation
        config = self.getDefaultOptions()
        config['views'] = []
        config['stream_views'] = [{'src': '//src_streams/rel*',
                                   'targ': '//targ_streams/rel*',
                                   'type': 'release',
                                   'parent': '//targ_streams/main'}]
        self.createConfigFile(options=config)

        msg = ""
        try:
            base_args = ['-c', self.transfer_cfg, '-s']
            pt = P4Transfer.P4Transfer(*base_args)
            pt.setupReplicate()
        except Exception as e:
            msg = str(e)
        self.assertRegex(msg, "Option transfer_target_stream must be specified if streams are being used")

        # Other streams related validation
        config = self.getDefaultOptions()
        config['views'] = []
        config['transfer_target_stream'] = ['//targ_streams/transfer_target_stream']
        config['stream_views'] = [{'fred': 'joe'}]
        self.createConfigFile(options=config)

        msg = ""
        try:
            base_args = ['-c', self.transfer_cfg, '-s']
            pt = P4Transfer.P4Transfer(*base_args)
            pt.setupReplicate()
        except Exception as e:
            msg = str(e)
        self.assertRegex(msg, "Missing required field 'src'")
        self.assertRegex(msg, "Missing required field 'targ'")
        self.assertRegex(msg, "Missing required field 'type'")
        self.assertRegex(msg, "Missing required field 'parent'")

        # Other streams related validation
        config = self.getDefaultOptions()
        config['views'] = []
        config['transfer_target_stream'] = ['//targ_streams/transfer_target_stream']
        config['stream_views'] = [{'src': '//src_streams/*rel*',
                                   'targ': '//targ_streams/rel*',
                                   'type': 'new',
                                   'parent': '//targ_streams/main'}]
        self.createConfigFile(options=config)

        msg = ""
        try:
            base_args = ['-c', self.transfer_cfg, '-s']
            pt = P4Transfer.P4Transfer(*base_args)
            pt.setupReplicate()
        except Exception as e:
            msg = str(e)
        self.assertRegex(msg, "Wildcards need to match")
        self.assertRegex(msg, "Stream type 'new' is not one of allowed values")

    def testChangeFormatting(self):
        "Formatting options for change descriptions"
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        desc = 'inside_file1 added'
        self.source.p4cmd('submit', '-d', desc)
        self.run_P4Transfer()
        self.assertCounters(1, 1)
        changes = self.target.p4cmd('changes', '-l', '-m1')
        self.assertRegex(changes[0]['desc'], "%s\n\nTransferred from p4://rsh:.*@1\n$" % desc)

        options = self.getDefaultOptions()
        options["change_description_format"] = "Originally $sourceChange by $sourceUser"
        self.createConfigFile(options=options)
        self.source.p4cmd('edit', inside_file1)
        desc = 'inside_file1 edited'
        self.source.p4cmd('submit', '-d', desc)
        self.run_P4Transfer()
        self.assertCounters(2, 2)
        changes = self.target.p4cmd('changes', '-l', '-m1')
        self.assertRegex(changes[0]['desc'], "Originally 2 by %s" % P4USER)

        options = self.getDefaultOptions()
        options["change_description_format"] = "Was $sourceChange by $sourceUser $fred\n$sourceDescription"
        self.createConfigFile(options=options)
        self.source.p4cmd('edit', inside_file1)
        desc = 'inside_file1 edited again'
        self.source.p4cmd('submit', '-d', desc)
        self.run_P4Transfer()
        self.assertCounters(3, 3)
        changes = self.target.p4cmd('changes', '-l', '-m1')
        self.assertEqual(changes[0]['desc'], "Was 3 by %s $fred\n%s\n" % (P4USER, desc))

    def testBatchSize(self):
        "Set batch size appropriately - make sure logging switches"
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'file added')

        for _ in range(1, 10):
            self.source.p4cmd('edit', inside_file1)
            append_to_file(inside_file1, "more")
            self.source.p4cmd('submit', '-d', 'edited')

        changes = self.source.p4cmd('changes', '-l', '-m1')
        self.assertEqual(changes[0]['change'], '10')

        options = self.getDefaultOptions()
        options["change_batch_size"] = "4"
        self.createConfigFile(options=options)

        self.run_P4Transfer()
        self.assertCounters(10, 10)

        logoutput = saved_stdoutput.getvalue()
        matches = re.findall("INFO: Logging to file:", logoutput)
        self.assertEqual(len(matches), 3)

    def testChangeMapFile(self):
        "How a change map file is written"
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'file added')
        self.run_P4Transfer()
        self.assertCounters(1, 1)

        options = self.getDefaultOptions()
        options["change_map_file"] = "depot/inside/change_map.csv"
        change_map_file = '//depot/import/change_map.csv'
        self.createConfigFile(options=options)

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'edited')
        self.run_P4Transfer()
        self.assertCounters(2, 3)
        change = self.target.p4.run_describe('4')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], change_map_file)
        content = self.target.p4.run_print('-q', change_map_file)[1]
        if python3:
            content = content.decode()
        content = content.split("\n")
        self.logger.debug("content:", content)
        self.assertRegex(content[0], "sourceP4Port,sourceChangeNo,targetChangeNo")
        self.assertRegex(content[1], "rsh.*,2,3")

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'edited again')
        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'and again')
        self.run_P4Transfer()
        self.assertCounters(4, 6)
        change = self.target.p4.run_describe('8')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], change_map_file)
        content = self.target.p4.run_print('-q', change_map_file)[1]
        if python3:
            content = content.decode()
        content = content.split("\n")
        self.logger.debug("content:", content)
        self.assertRegex(content[1], "rsh.*,2,3")
        self.assertRegex(content[2], "rsh.*,3,6")
        self.assertRegex(content[3], "rsh.*,4,7")

    def testChangeMapFileNotRoot(self):
        "How a change map file is written - when not at root"
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'file added')
        self.run_P4Transfer()
        self.assertCounters(1, 1)

        options = self.getDefaultOptions()
        options["change_map_file"] = "depot/inside/changes/change_map.csv"
        change_map_file = '//depot/import/changes/change_map.csv'
        self.createConfigFile(options=options)

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'edited')
        self.run_P4Transfer()
        self.assertCounters(2, 3)
        change = self.target.p4.run_describe('4')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], change_map_file)
        content = self.target.p4.run_print('-q', change_map_file)[1]
        if python3:
            content = content.decode()
        content = content.split("\n")
        self.logger.debug("content:", content)
        self.assertRegex(content[0], "sourceP4Port,sourceChangeNo,targetChangeNo")
        self.assertRegex(content[1], "rsh.*,2,3")

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'edited again')
        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'and again')
        self.run_P4Transfer()
        self.assertCounters(4, 6)
        change = self.target.p4.run_describe('8')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], change_map_file)
        content = self.target.p4.run_print('-q', change_map_file)[1]
        if python3:
            content = content.decode()
        content = content.split("\n")
        self.logger.debug("content:", content)
        self.assertRegex(content[1], "rsh.*,2,3")
        self.assertRegex(content[2], "rsh.*,3,6")
        self.assertRegex(content[3], "rsh.*,4,7")

    def testArchive(self):
        "Archive a file"
        self.setupTransfer()

        d = self.source.p4.fetch_depot('archive')
        d['Type'] = 'archive'
        self.source.p4.save_depot(d)

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        create_file(inside_file1, 'Test content')
        create_file(inside_file2, 'Test content')
        create_file(inside_file3, 'Test content')
        self.source.p4cmd('add', '-tbinary', inside_file1)
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('add', '-tbinary', inside_file3)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('edit', inside_file2)
        self.source.p4cmd('edit', inside_file3)
        append_to_file(inside_file1, "Some text")
        append_to_file(inside_file2, "More text")
        append_to_file(inside_file3, "More text")
        self.source.p4cmd('submit', '-d', 'files edited')

        self.source.p4cmd('archive', '-D', 'archive', inside_file1)
        filelog = self.source.p4.run_filelog('//depot/inside/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'archive')
        self.assertEqual(filelog[0].revisions[1].action, 'archive')
        self.source.p4cmd('archive', '-D', 'archive', inside_file3 + "#1")
        filelog = self.source.p4.run_filelog('//depot/inside/inside_file3')
        self.assertEqual(filelog[0].revisions[0].action, 'edit')
        self.assertEqual(filelog[0].revisions[1].action, 'archive')

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        change = self.target.p4.run_describe('1')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/inside_file2')
        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/inside_file2')
        self.assertEqual(change['depotFile'][1], '//depot/import/inside_file3')

    def testAdd(self):
        "Basic file add"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 1, "Target does not have exactly one change")
        self.assertEqual(changes[0]['change'], "1")

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['depotFile'], '//depot/import/inside_file1')

        self.assertCounters(1, 1)

    def testNonSuperUser(self):
        "Test when not a superuser - who can't update"
        self.setupTransfer()
        # Setup default transfer user as super user
        # All other users will just have write access
        username = "newuser"
        p = self.target.p4.fetch_protect()
        p['Protections'].append("review user %s * //..." % username)
        self.target.p4.save_protect(p)

        options = self.getDefaultOptions()
        options["superuser"] = "n"
        targOptions = {"p4user": username}
        self.createConfigFile(options=options, targOptions=targOptions)

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 1, "Target does not have exactly one change")
        self.assertEqual(changes[0]['change'], "1")

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['depotFile'], '//depot/import/inside_file1')

        self.assertCounters(1, 1)

    def testObliterate(self):
        "What happens to obliterated changes"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, 'Test content')
        create_file(inside_file2, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "more stuff")
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 edited and 2 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "yet more stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited again')

        self.source.p4cmd('obliterate', '-y', "%s#2,2" % inside_file1)

        self.run_P4Transfer()

        self.assertCounters(3, 3)

        changes = self.target.p4cmd('changes')
        self.assertEqual(3, len(changes))
        # self.assertEqual(changes[0]['change'], "1")

        # files = self.target.p4cmd('files', '//depot/...')
        # self.assertEqual(len(files), 1)
        # self.assertEqual(files[0]['depotFile'], '//depot/import/inside_file1')

    def testSymlinks(self):
        "Various symlink actions"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        file_link1 = os.path.join(inside, "link1")
        file_link2 = os.path.join(inside, "link2")
        file_link3 = os.path.join(inside, "link3")
        file_link4 = os.path.join(inside, "link4")
        file_link5 = os.path.join(inside, "link5")
        file_link6 = os.path.join(inside, "link6")
        inside_file2 = os.path.join(inside, "subdir", "inside_file2")
        create_file(inside_file1, '01234567890123456789012345678901234567890123456789')
        create_file(inside_file2, '01234567890123456789012345678901234567890123456789more')
        if os.name == "nt":
            create_file(file_link1, "inside_file1\n")
            create_file(file_link2, "subdir\n")
        else:
            os.symlink("inside_file1", file_link1)
            os.symlink("subdir", file_link2)

        self.source.p4cmd('add', inside_file1, inside_file2)
        self.source.p4cmd('add', "-t", "symlink", file_link1, file_link2)
        self.source.p4cmd('submit', '-d', 'files added')

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 1, "Target does not have exactly one change")
        self.assertEqual(changes[0]['change'], "1")

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 4)
        self.assertEqual(files[0]['depotFile'], '//depot/import/inside_file1')

        self.source.p4cmd('edit', file_link1, file_link2)
        self.source.p4cmd('move', file_link1, file_link3)
        self.source.p4cmd('move', file_link2, file_link4)
        self.source.p4cmd('submit', '-d', 'links moved')

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        self.source.p4cmd('edit', file_link3, file_link4)
        self.source.p4cmd('move', file_link3, file_link5)
        self.source.p4cmd('move', file_link4, file_link6)
        self.source.p4cmd('submit', '-d', 'links moved')

        self.run_P4Transfer()
        self.assertCounters(3, 3)

    def testUTF16FaultyBOM(self):
        "UTF 16 file with faulty BOM"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        fname = "data-file-utf16"
        rcs_fname = fname + ",v"
        inside_file1 = os.path.join(inside, fname)

        create_file(inside_file1, 'Test content')
        self.source.p4cmd('add', '-t', 'utf16', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        # Now we substitute the depot file with test data
        depot_rcs_fname = os.path.join(self.source.server_root, 'depot', 'inside', rcs_fname)
        shutil.copy(rcs_fname, depot_rcs_fname)
        self.source.p4cmd('verify', '-q', '//depot/inside/...')
        with self.source.p4.at_exception_level(P4.P4.RAISE_ERRORS):
            self.source.p4cmd('verify', '-qv', '//depot/inside/...')
        self.source.p4cmd('verify', '-q', '//depot/inside/...')

        self.run_P4Transfer()
        self.assertCounters(1, 1)

    @unittest.skip('Not yet working')
    def testAncientVersion(self):
        "An ancient r99.2 version repository - no filesize/digest present"
        self.setupTransfer()

        # Restore from ancient checkpoint
        ckp = os.path.join(os.getcwd(), "test_data_r99", "checkpoint.r99")
        cmd = '%s -r "%s" -jr "%s"' % (self.source.p4d, self.source.server_root, ckp)
        self.logger.debug("Cmd: %s" % cmd)
        output = subprocess.check_output(cmd, shell=True, universal_newlines=True)
        self.logger.debug("Output: %s" % output)

        # Upgrade DB
        cmd = '%s -r "%s" -J journal -xu' % (self.source.p4d, self.source.server_root)
        self.logger.debug("Cmd: %s" % cmd)
        output = subprocess.check_output(cmd, shell=True, universal_newlines=True)
        self.logger.debug("Output: %s" % output)

        rcs_dir = localDirectory(self.source.server_root, "depot", "inside")
        for f in glob.glob(os.path.join("test_data_r99", "*,v")):
            shutil.copy(f, rcs_dir)

        self.run_P4Transfer()
        self.assertCounters(1, 1)

    @unittest.skip("P4 now seems able to sync this file!")
    def testUTF16Unsyncable(self):
        "UTF 16 file which can't be synced"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        fname = "data-file-utf16-unsyncable"
        rcs_fname = fname + ",v"
        inside_file1 = os.path.join(inside, fname)

        create_file(inside_file1, 'Test content')
        self.source.p4cmd('add', '-t', 'utf16', inside_file1)
        self.source.p4cmd('submit', '-d', 'file added')

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('submit', '-d', 'file edited')

        self.source.p4cmd('edit', '-t', 'binary', inside_file1)
        append_to_file(inside_file1, "Test content")
        self.source.p4cmd('submit', '-d', 'file added')

        # Now we substitute the depot file with test data
        depot_rcs_fname = os.path.join(self.source.server_root, 'depot', 'inside', rcs_fname)
        shutil.copy(rcs_fname, depot_rcs_fname)
        self.source.p4cmd('verify', '-q', '//depot/inside/...')
        with self.source.p4.at_exception_level(P4.P4.RAISE_ERRORS):
            self.source.p4cmd('verify', '-qv', '//depot/inside/...')
        self.source.p4cmd('verify', '-q', '//depot/inside/...')

        self.run_P4Transfer()
        self.assertCounters(0, 0)

        self.source.p4cmd('retype', '-t', 'binary', '//depot/inside/...@1,@2')
        self.run_P4Transfer()
        self.assertCounters(3, 3)

    @unittest.skipIf(python3 and (platform.system().lower() == "windows"),
                     "Unicode not supported in Python3 on Windows yet - works on Mac/Unix...")
    @unittest.skipIf(not python3 and platform.system().lower() == 'darwin',
                     'undecodable name cannot always be decoded on macOS')
    def testUnicode(self):
        "Adding of files with Unicode filenames"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        if python3:
            inside_file1 = "inside_file1uåäö"

        else:
            inside_file1 = u"inside_file1uåäö".encode(sys.getfilesystemencoding())
        inside_file2 = "Am\xE8lioration.txt"
        localinside_file1 = os.path.join(inside, inside_file1)
        localinside_file2 = os.path.join(inside, inside_file2)

        create_file(localinside_file1, 'Test content')
        create_file(localinside_file2, 'Some Test content')
        self.source.p4cmd('add', '-f', localinside_file1)
        self.source.p4cmd('add', '-f', localinside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['change'], "1")

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 2)
        self.assertEqual(files[1]['depotFile'], '//depot/import/%s' % inside_file1)

        self.assertCounters(1, 1)

    # def testEncodingError(self):
    #     "Adding of files with Unicode encoding errors in filename"
    #     self.setupTransfer()
    #
    #     inside = localDirectory(self.source.client_root, "inside")
    #     # if python3:
    #     inside_file1 = b"inside_file1\x92s".decode('cp1250')
    #     inside_file2 = "50�s table"
    #     # else:
    #     #     inside_file1 = u"inside_file1’s".encode(sys.getfilesystemencoding())
    #     localinside_file1 = os.path.join(inside, inside_file1)
    #     localinside_file2 = os.path.join(inside, inside_file2)
    #
    #     create_file(localinside_file1, 'Test content')
    #     create_file(localinside_file2, 'Test content')
    #     self.source.p4cmd('add', '-f', localinside_file1)
    #     self.source.p4cmd('add', '-f', localinside_file2)
    #     self.source.p4cmd('submit', '-d', 'inside_files added')
    #
    #     self.run_P4Transfer()
    #
    #     changes = self.target.p4cmd('changes')
    #     self.assertEqual(len(changes), 1)
    #     self.assertEqual(changes[0]['change'], "1")
    #
    #     files = self.target.p4cmd('files', '//depot/...')
    #     self.assertEqual(len(files), 2)
    #     self.assertEqual(files[1]['depotFile'], '//depot/import/%s' % inside_file1)
    #     self.assertEqual(files[0]['depotFile'], '//depot/import/%s' % inside_file2)
    #
    #     self.assertCounters(1, 1)

    def testWildcardChars(self):
        "Test filenames containing Perforce wildcards"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")
        inside_file1 = os.path.join(inside, "@inside_file1")
        inside_file2 = os.path.join(inside, "%inside_file2")
        inside_file3 = os.path.join(inside, "#inside_file3")
        inside_file4 = os.path.join(inside, "C#", "inside_file4")
        outside_file1 = os.path.join(outside, "%outside_file")

        inside_file1Fixed = inside_file1.replace("@", "%40")
        inside_file2Fixed = inside_file2.replace("%", "%25")
        inside_file3Fixed = inside_file3.replace("#", "%23")
        inside_file4Fixed = inside_file4.replace("#", "%23")
        outside_file1Fixed = outside_file1.replace("%", "%25")

        create_file(inside_file1, 'Test content')
        create_file(inside_file3, 'Test content')
        create_file(inside_file4, 'Test content')
        create_file(outside_file1, 'Test content')
        self.source.p4cmd('add', '-f', inside_file1)
        self.source.p4cmd('add', '-f', inside_file3)
        self.source.p4cmd('add', '-f', inside_file4)
        self.source.p4cmd('add', '-f', outside_file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('integrate', outside_file1Fixed, inside_file2Fixed)
        self.source.p4cmd('submit', '-d', 'files integrated')

        self.source.p4cmd('edit', inside_file1Fixed)
        self.source.p4cmd('edit', inside_file3Fixed)
        self.source.p4cmd('edit', inside_file4Fixed)
        append_to_file(inside_file1, 'Different stuff')
        append_to_file(inside_file3, 'Different stuff')
        append_to_file(inside_file4, 'Different stuff')
        self.source.p4cmd('submit', '-d', 'files modified')

        self.source.p4cmd('integrate', "//depot/inside/*", "//depot/inside/new/*")
        self.source.p4cmd('submit', '-d', 'files branched')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 7)
        self.assertEqual(files[0]['depotFile'], '//depot/import/%23inside_file3')
        self.assertEqual(files[1]['depotFile'], '//depot/import/%25inside_file2')
        self.assertEqual(files[2]['depotFile'], '//depot/import/%40inside_file1')
        self.assertEqual(files[3]['depotFile'], '//depot/import/C%23/inside_file4')

    def testEditAndDelete(self):
        "Edits and Deletes"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', "inside_file1 added")

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, 'More content')
        self.source.p4cmd('submit', '-d', "inside_file1 edited")

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes', )
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0]['change'], "2")

        self.assertCounters(2, 2)

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', "inside_file1 deleted")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        changes = self.target.p4cmd('changes', )
        self.assertEqual(len(changes), 3, "Target does not have exactly three changes")
        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'delete', "Target has not been deleted")

        create_file(inside_file1, 'New content')
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', "Re-added")

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'add')

    def testFileTypes(self):
        "File types are transferred appropriately"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-tbinary', inside_file1)
        self.source.p4cmd('submit', '-d', "inside_file1 added")

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].type, 'binary')

        self.source.p4cmd('edit', '-t+x', inside_file1)
        append_to_file(inside_file1, "More content")
        self.source.p4cmd('submit', '-d', "Type changed")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertTrue(filelog[0].revisions[0].type in ['xbinary', 'binary+x'])

        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file2, "$Id$\n$DateTime$")
        self.source.p4cmd('add', '-t+k', inside_file2)
        self.source.p4cmd('submit', '-d', "Ktext added")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertTrue(filelog[0].revisions[0].type in ['ktext', 'text+k'])
        verifyResult = self.target.p4.run_verify('-q', '//depot/import/inside_file2')
        self.assertEqual(len(verifyResult), 0)  # just to see that ktext gets transferred properly

        content = self.target.p4.run_print('//depot/import/inside_file2')[1]
        if python3:
            content = content.decode()
        lines = content.split("\n")
        self.assertEqual(lines[0], '$Id: //depot/import/inside_file2#1 $')

    def testFileTypesPlusL(self):
        "File types are transferred appropriately even when exclusive locked"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-tbinary', inside_file1)
        self.source.p4cmd('submit', '-d', "inside_file1 added")

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].type, 'binary')

        self.source.p4cmd('edit', '-t+l', inside_file1)
        append_to_file(inside_file1, "More content")
        self.source.p4cmd('submit', '-d', "Type changed")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertTrue(filelog[0].revisions[0].type in ['binary+l'])

    def testFileTypesPlusLCommit(self):
        "File types are transferred appropriately even when exclusive locked on a commit-server"
        self.setupTransfer()

        # Change target to a commit server
        self.target.p4cmd('serverid', 'mycommit')
        svr = self.target.p4.fetch_server('mycommit')
        svr['Services'] = 'commit-server'
        self.target.p4.save_server(svr)

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-ttext', inside_file1)
        self.source.p4cmd('submit', '-d', "inside_file1 added")

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].type, 'text')

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('move', inside_file1, inside_file2)
        self.source.p4cmd('reopen', '-tbinary+l', inside_file2)
        # append_to_file(inside_file1, "More content")
        self.source.p4cmd('submit', '-d', "Type changed")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].type, 'binary+l')

    def testFileTypeIntegrations(self):
        "File types are integrated appropriately"
        self.setupTransfer()
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")
        self.source.p4cmd("configure", "set", "dm.integ.engine=2")

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        inside_file4 = os.path.join(inside, "inside_file4")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-ttext', inside_file1)
        self.source.p4cmd('submit', '-d', "inside_file1 added")

        self.source.p4cmd('integ', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', "inside_file2 added")

        self.source.p4cmd('integ', inside_file2, inside_file3)
        self.source.p4cmd('integ', inside_file2, inside_file4)
        self.source.p4cmd('reopen', '-ttext', inside_file4)
        self.source.p4cmd('submit', '-d', "files added")

        self.source.p4cmd('edit', '-tbinary', inside_file1)
        append_to_file(inside_file1, "More content")
        self.source.p4cmd('submit', '-d', "Type changed")

        self.source.p4cmd('integ', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('submit', '-d', "type change integrated")

        self.source.p4cmd('integ', inside_file2, inside_file3)
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('submit', '-d', "type change integrated")

        self.source.p4cmd('integ', inside_file2, inside_file4)
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('reopen', '-ttext', inside_file4)
        self.source.p4cmd('submit', '-d', "type change NOT integrated")

        self.run_P4Transfer()
        self.assertCounters(7, 7)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].type, 'binary')
        filelog = self.target.p4.run_filelog('//depot/import/inside_file3')
        self.assertEqual(filelog[0].revisions[0].type, 'binary')
        filelog = self.target.p4.run_filelog('//depot/import/inside_file4')
        self.assertEqual(filelog[0].revisions[0].type, 'text')

    def testMoveObliteratedDelete(self):
        """Test for Move where deleted file has an obliterated version"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', '-tbinary', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.source.p4cmd('obliterate', '-y', "%s#1" % original_file)

        self.run_P4Transfer()
        self.assertCounters(2, 1)

    def testMoveObliteratedSource(self):
        """Test for Move where source file has been obliterated"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', '-tbinary', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.source.p4cmd('obliterate', '-y', original_file)

        self.run_P4Transfer()
        self.assertCounters(2, 1)

    def testMoveBinary(self):
        """Test for Move"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', '-tbinary', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        change = self.target.p4.run_describe('1')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/original/original_file')

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/new/new_file')
        self.assertEqual(change['depotFile'][1], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'move/add')
        self.assertEqual(change['action'][1], 'move/delete')

        self.source.p4cmd('edit', renamed_file)
        self.source.p4cmd('move', renamed_file, original_file)
        self.source.p4cmd('submit', '-d', "renaming file back")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

    def testMoveWithCopyFromOutside(self):
        """Test for Move and Copy to same file where the Copy is ignored"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        # Note _outside sorts before inside - important to provoke a problem
        outside = localDirectory(self.source.client_root, "_outside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        other_file = os.path.join(outside, 'new', 'new_file')
        create_file(original_file, "Some content")
        create_file(other_file, "Some content")
        self.source.p4cmd('add', '-tbinary', original_file, other_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('integ', '-f', other_file, renamed_file)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', "renaming file with copy")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/new/new_file')
        self.assertEqual(change['depotFile'][1], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'move/add')
        self.assertEqual(change['action'][1], 'move/delete')
        filelog = self.target.p4.run_filelog('//depot/import/new/new_file')
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'moved from')

    # def testPartialTransferMoves(self):
    #     """Test for Move when partial_transfer=y"""
    #     self.setupTransfer()
    #     inside = localDirectory(self.source.client_root, "inside")
    #     outside = localDirectory(self.source.client_root, "outside")
    #
    #     original_file = os.path.join(inside, 'original', 'original_file')
    #     renamed_file = os.path.join(inside, 'new', 'new_file')
    #     create_file(original_file, "Some content")
    #     self.source.p4cmd('add', original_file)
    #     self.source.p4cmd('submit', '-d', "adding original file")
    #
    #     self.source.p4cmd('edit', original_file)
    #     append_to_file(original_file, "new lines")
    #     self.source.p4cmd('submit', '-d', "editing original file")
    #
    #     self.source.p4cmd('edit', original_file)
    #     self.source.p4cmd('move', original_file, renamed_file)
    #     self.source.p4cmd('submit', '-d', "renaming file")
    #
    #     # Now we partially transfer
    #     self.target.p4cmd('counter', TEST_COUNTER_NAME, '1')
    #
    #     options = {"partial_transfer": 'y'}
    #     self.createConfigFile(options=options)
    #
    #     self.run_P4Transfer()
    #     self.assertCounters(2, 2)

    def testMoves(self):
        """Test for Move and then a file being moved back, also move inside<->outside"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        change = self.target.p4.run_describe('1')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/original/original_file')

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/new/new_file')
        self.assertEqual(change['depotFile'][1], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'move/add')
        self.assertEqual(change['action'][1], 'move/delete')

        self.source.p4cmd('edit', renamed_file)
        self.source.p4cmd('move', renamed_file, original_file)
        self.source.p4cmd('submit', '-d', "renaming file back")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/new/new_file')
        self.assertEqual(change['depotFile'][1], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'move/delete')
        self.assertEqual(change['action'][1], 'move/add')

        # Now move inside to outside
        outside_file = os.path.join(outside, 'outside_file')
        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, outside_file)
        self.source.p4cmd('submit', '-d', "moving file outside")

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        change = self.target.p4.run_describe('4')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'delete')

        # Now move outside to inside
        self.source.p4cmd('edit', outside_file)
        self.source.p4cmd('move', outside_file, original_file)
        self.source.p4cmd('submit', '-d', "moving file from outside back to inside")

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        change = self.target.p4.run_describe('5')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'add')

    def testMoveCopyCombo(self):
        """Test for Move where the add also has a copy"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        file2 = os.path.join(inside, 'new', 'file2')
        create_file(original_file, "Some content\n")
        create_file(file2, "Other content\n")
        self.source.p4cmd('add', original_file, file2)
        self.source.p4cmd('submit', '-d', "adding original files")

        self.source.p4cmd('edit', original_file)
        append_to_file(original_file, 'More\n')
        self.source.p4cmd('submit', '-d', "editing original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('integ', file2, renamed_file)
        self.source.p4cmd('resolve', '-at', renamed_file)
        self.source.p4cmd('submit', '-d', "renaming and copying file")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(2, len(change['depotFile']))
        self.assertEqual('//depot/import/new/new_file', change['depotFile'][0])
        self.assertEqual('//depot/import/original/original_file', change['depotFile'][1])
        self.assertEqual('move/add', change['action'][0])
        self.assertEqual('move/delete', change['action'][1])
        filelog = self.target.p4.run_filelog('//depot/import/new/new_file')
        self.assertEqual(2, len(filelog[0].revisions[0].integrations))
        self.assertEqual("copy from", filelog[0].revisions[0].integrations[0].how)
        self.assertEqual("moved from", filelog[0].revisions[0].integrations[1].how)

    def testMoveCopyIgnoreCombo(self):
        """Test for Move where the add also has a copy and an ignore"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        file2 = os.path.join(inside, 'new', 'file2')
        file3 = os.path.join(inside, 'new', 'file3')
        create_file(original_file, "Some content\n")
        create_file(file2, "Other content\n")
        create_file(file3, "Some Other content\n")
        self.source.p4cmd('add', original_file, file2, file3)
        self.source.p4cmd('submit', '-d', "adding original files")

        self.source.p4cmd('edit', original_file)
        append_to_file(original_file, 'More\n')
        self.source.p4cmd('submit', '-d', "editing original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('integ', file3, renamed_file)
        self.source.p4cmd('resolve', '-ay', renamed_file)
        self.source.p4cmd('integ', file2, renamed_file)
        self.source.p4cmd('resolve', '-at', renamed_file)
        self.source.p4cmd('submit', '-d', "rename/copy/ignore file")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(2, len(change['depotFile']))
        self.assertEqual('//depot/import/new/new_file', change['depotFile'][0])
        self.assertEqual('//depot/import/original/original_file', change['depotFile'][1])
        self.assertEqual('move/add', change['action'][0])
        self.assertEqual('move/delete', change['action'][1])
        filelog = self.target.p4.run_filelog('//depot/import/new/new_file')
        self.assertEqual(3, len(filelog[0].revisions[0].integrations))
        self.assertEqual("copy from", filelog[0].revisions[0].integrations[0].how)
        self.assertEqual("ignored", filelog[0].revisions[0].integrations[1].how)
        self.assertEqual("moved from", filelog[0].revisions[0].integrations[2].how)

    def testOldStyleMove(self):
        """Old style move - a branch and delete"""
        self.setupTransfer()
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")
        self.source.p4cmd("configure", "set", "dm.integ.engine=2")
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'dir', 'build-tc.sh')
        renamed_file = os.path.join(inside, 'dir', 'build.sh')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('integ', "%s#2" % original_file, renamed_file)
        self.source.p4cmd('edit', renamed_file)
        append_to_file(renamed_file, 'appendage')
        self.source.p4cmd('delete', original_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/dir/build-tc.sh')
        self.assertEqual(change['depotFile'][1], '//depot/import/dir/build.sh')
        self.assertEqual(change['action'][0], 'delete')
        self.assertEqual(change['action'][1], 'add')

    def testMoveMoveBack(self):
        """Test for Move and then a file being moved back, also move inside<->outside"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'main', 'original', 'file1')
        renamed_file = os.path.join(inside, 'main', 'renamed', 'file1')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('integrate', '//depot/inside/main/...', '//depot/inside/branch/...')
        self.source.p4cmd('submit', '-d', "branching file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.source.p4cmd('integrate', '//depot/inside/main/...', '//depot/inside/branch/...')
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('submit', '-d', "branching rename and rename back file")

        self.source.p4cmd('edit', renamed_file)
        self.source.p4cmd('move', renamed_file, original_file)
        self.source.p4cmd('submit', '-d', "renaming file back")

        # Copy the individual move/add and move/delete files, which become add and delete like this
        with self.source.p4.at_exception_level(P4.P4.RAISE_ERRORS):
            self.source.p4cmd('copy', '//depot/inside/main/original/file1', '//depot/inside/branch/original/file1')
            self.source.p4cmd('copy', '//depot/inside/main/renamed/file1', '//depot/inside/branch/renamed/file1')
        self.source.p4cmd('submit', '-d', "copying rename back to other branch individually")

        self.run_P4Transfer()
        self.assertCounters(6, 6)

        change = self.target.p4.run_describe('6')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/branch/original/file1')
        self.assertEqual(change['depotFile'][1], '//depot/import/branch/renamed/file1')
        self.assertEqual(change['action'][0], 'branch')
        self.assertEqual(change['action'][1], 'delete')

    def testMoveAfterDelete(self):
        """Test for Move after a Delete"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, 'file1')
        file2 = os.path.join(inside, 'file2')
        create_file(file1, "Some content")
        self.source.p4cmd("add", file1)
        self.source.p4cmd("submit", '-d', "adding original file")

        self.source.p4cmd("delete", file1)
        self.source.p4cmd("submit", '-d', "deleting original file")

        self.source.p4cmd("sync", "%s#1" % file1)
        self.source.p4cmd("edit", file1)
        self.source.p4cmd("move", file1, file2)
        try:
            self.source.p4cmd("resolve")
        except Exception:
            pass
        try:
            self.source.p4cmd("submit", '-d', "renaming old version of original file")
        except Exception:
            pass

        self.source.p4cmd("sync")
        self.source.p4cmd("submit", "-c3")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/file1')
        self.assertEqual(change['depotFile'][1], '//depot/import/file2')
        self.assertEqual(change['action'][0], 'move/delete')
        self.assertEqual(change['action'][1], 'move/add')

    def testMoveAfterDeleteAndEdit(self):
        """Test for Move after a Delete when file content is also changed"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, 'file1')
        file2 = os.path.join(inside, 'file2')
        create_file(file1, "Some content")
        self.source.p4cmd("add", file1)
        self.source.p4cmd("submit", '-d', "adding original file")

        self.source.p4cmd("delete", file1)
        self.source.p4cmd("submit", '-d', "deleting original file")

        self.source.p4cmd("sync", "%s#1" % file1)
        self.source.p4cmd("edit", file1)
        self.source.p4cmd("move", file1, file2)
        try:
            self.source.p4cmd("resolve")
        except Exception:
            pass
        try:
            self.source.p4cmd("submit", '-d', "renaming old version of original file")
        except Exception:
            pass

        self.source.p4cmd("sync")
        append_to_file(file2, "A change")
        self.source.p4cmd("submit", "-c3")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/file1')
        self.assertEqual(change['depotFile'][1], '//depot/import/file2')
        self.assertEqual(change['action'][0], 'move/delete')
        self.assertEqual(change['action'][1], 'move/add')

    def testMoveFromOutsideAndEdit(self):
        """Test for Move from outside with subsequent edit of a file"""
        self.setupTransfer()

        depot = self.target.p4.fetch_depot('target')
        self.target.p4.save_depot(depot)

        # Temporarily create different source client
        source_client = self.source.p4.fetch_client(self.source.p4.client)
        source_client._view = ['//depot/inside/main/Dir/... //%s/main/Dir/...' % self.source.p4.client,
                               '//depot/outside/... //%s/outside/...' % self.source.p4.client]
        self.source.p4.save_client(source_client)

        inside = localDirectory(self.source.client_root, "main", "Dir")
        outside = localDirectory(self.source.client_root, "outside")

        original_file1 = os.path.join(outside, 'original_file1')
        original_file2 = os.path.join(outside, 'original_file2')
        renamed_file1 = os.path.join(inside, 'new_file1')
        renamed_file2 = os.path.join(inside, 'new_file2')
        create_file(original_file1, "Some content")
        create_file(original_file2, "Some content")
        self.source.p4cmd('add', original_file1, original_file2)
        self.source.p4cmd('submit', '-d', "adding original files")

        self.source.p4cmd('edit', original_file1, original_file2)
        self.source.p4cmd('move', original_file1, renamed_file1)
        self.source.p4cmd('move', original_file2, renamed_file2)
        self.source.p4cmd('submit', '-d', "renaming files")

        source_client = self.source.p4.fetch_client(self.source.p4.client)
        source_client._view = ['//depot/inside/main/Dir/... //%s/main/Dir/...' % self.source.p4.client]
        self.source.p4.save_client(source_client)

        self.source.p4cmd('edit', renamed_file1)
        self.source.p4cmd('edit', renamed_file2)
        self.source.p4cmd('submit', '-d', "editing file")

        self.source.p4cmd('delete', renamed_file1)
        self.source.p4cmd('delete', renamed_file2)
        self.source.p4cmd('submit', '-d', "deleting file")

        config = self.getDefaultOptions()
        config['views'] = [{'src': '//depot/inside/main/Dir/...',
                            'targ': '//target/inside/main/Dir/...'}]
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(4, 3)

        change = self.target.p4.run_describe('1')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//target/inside/main/Dir/new_file1')
        self.assertEqual(change['depotFile'][1], '//target/inside/main/Dir/new_file2')
        self.assertEqual(change['action'][0], 'add')
        self.assertEqual(change['action'][1], 'add')

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//target/inside/main/Dir/new_file1')
        self.assertEqual(change['depotFile'][1], '//target/inside/main/Dir/new_file2')
        self.assertEqual(change['action'][0], 'edit')
        self.assertEqual(change['action'][1], 'edit')

    def testMoveAndCopy(self):
        """Test for Move with subsequent copy of a file"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        branched_file = os.path.join(inside, 'branch', 'new_file')
        create_file(original_file, "Some content")
        self.source.p4cmd('add', original_file)
        self.source.p4cmd('submit', '-d', "adding original file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.source.p4cmd('integrate', '-Di', renamed_file, branched_file)
        self.source.p4cmd('submit', '-d', "copying files")

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/new/new_file')
        self.assertEqual(change['depotFile'][1], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'move/add')
        self.assertEqual(change['action'][1], 'move/delete')

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/branch/new_file')
        self.assertEqual(change['action'][0], 'branch')

    def testMoveAndIntegrate(self):
        """Test for Move with a merge - requires add -d"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        original_file = os.path.join(inside, 'original', 'original_file')
        renamed_file = os.path.join(inside, 'new', 'new_file')
        other_file = os.path.join(inside, 'branch', 'new_file')
        create_file(original_file, "Some content")
        create_file(other_file, "Some content\nnew")
        self.source.p4cmd('add', original_file, other_file)
        self.source.p4cmd('submit', '-d', "adding original and other file")

        self.source.p4cmd('edit', original_file)
        self.source.p4cmd('move', original_file, renamed_file)
        self.source.p4cmd('integ', '-f', other_file, renamed_file)
        self.source.p4cmd('resolve', '-am', renamed_file)
        self.source.p4cmd('submit', '-d', "renaming file")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 2)
        self.assertEqual(change['depotFile'][0], '//depot/import/new/new_file')
        self.assertEqual(change['depotFile'][1], '//depot/import/original/original_file')
        self.assertEqual(change['action'][0], 'move/add')
        self.assertEqual(change['action'][1], 'move/delete')

    def testUndo(self):
        "Simple undo of add/edit"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 deleted')

        self.source.p4cmd('undo', "%s#2" % inside_file1)
        self.source.p4cmd('submit', '-d', 'undo delete')

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "More content")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('undo', "%s#4" % inside_file1)
        # self.source.p4cmd('resolve', '-ay')
        self.source.p4cmd('submit', '-d', 'undo edit')

        self.run_P4Transfer()
        self.assertCounters(5, 5)

    def testBranchEditBinary(self):
        "Branch and edit binary file"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-tbinary', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integ', inside_file1, inside_file2)
        self.source.p4cmd('edit', inside_file2)
        append_to_file(inside_file2, "\nmore")
        self.source.p4cmd('submit', '-d', 'inside_file2 created')

        filelog = self.source.p4cmd('filelog', '//...')
        self.assertEqual(2, len(filelog))
        self.assertNotEqual(filelog[0]['digest'][0], filelog[1]['digest'][0])
        self.assertNotEqual(filelog[0]['fileSize'][0], filelog[1]['fileSize'][0])

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4cmd('filelog', '//...')
        self.assertEqual(2, len(filelog))
        self.assertNotEqual(filelog[0]['digest'][0], filelog[1]['digest'][0])
        self.assertNotEqual(filelog[0]['fileSize'][0], filelog[1]['fileSize'][0])

    def testBranchIgnoreBinary(self):
        "Branch with ignore binary file"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")

        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-tbinary', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        create_file(inside_file2, "Other content")
        self.source.p4cmd('add', '-tbinary', inside_file2)

        self.source.p4cmd('integ', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-ay')
        self.source.p4cmd('submit', '-d', 'inside_file2 created')

        filelog = self.source.p4cmd('filelog', '//...')
        self.assertEqual(2, len(filelog))
        self.assertNotEqual(filelog[0]['digest'][0], filelog[1]['digest'][0])
        self.assertNotEqual(filelog[0]['fileSize'][0], filelog[1]['fileSize'][0])

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4cmd('filelog', '//...')
        self.assertEqual(2, len(filelog))
        self.assertNotEqual(filelog[0]['digest'][0], filelog[1]['digest'][0])
        self.assertNotEqual(filelog[0]['fileSize'][0], filelog[1]['fileSize'][0])
        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, "ignored")

    def testSimpleIntegrate(self):
        "Simple integration options - inside client workspace view"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 2)

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "More content")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2 (copy)')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(len(filelog[0].revisions), 2)
        self.assertEqual(len(filelog[0].revisions[1].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, "copy from")

        # Now make 2 changes and integrate them one at a time.
        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "More content2")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "More content3")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('integrate', inside_file1 + "#3", inside_file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2 (copy)')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2 (copy)')

        self.run_P4Transfer()
        self.assertCounters(8, 8)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.logger.debug(filelog)
        self.assertEqual(len(filelog[0].revisions), 4)
        self.assertEqual(len(filelog[0].revisions[1].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, "copy from")

    def testHistoricalStartSimple(self):
        "Simple integration options for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        contents = ["0"] * 10
        contents2 = contents[:]
        create_file(file1, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        file2 = os.path.join(inside, "file2")
        self.source.p4cmd('integrate', file1, file2)
        self.source.p4cmd('submit', '-d', '2: file1 -> file2')

        self.source.p4cmd('edit', file1)
        self.source.p4cmd('edit', file2)
        contents[0] = "file1"
        create_file(file1, "\n".join(contents) + "\n")
        contents2[5] = "file2"
        create_file(file2, "\n".join(contents2) + "\n")
        self.source.p4cmd('submit', '-d', '3: file1 edited')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4.run_resolve('-am')
        self.source.p4cmd('submit', '-d', '4: file1 -> file2 (merge)')

        filelog = self.source.p4.run_filelog('//depot/inside/file2')
        self.assertEqual("merge from", filelog[0].revisions[0].integrations[0].how)

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '4'
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(4, 1)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(1, len(filelog[0].revisions))
        self.assertEqual(0, len(filelog[0].revisions[0].integrations))
        self.assertEqual("add", filelog[0].revisions[0].action)

    def testHistoricalStartSimple2(self):
        "Simple integration options for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        contents = ["0"] * 10
        contents2 = contents[:]
        create_file(file1, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', 'file1 added')

        file2 = os.path.join(inside, "file2")
        self.source.p4cmd('integrate', file1, file2)
        self.source.p4cmd('submit', '-d', 'file1 -> file2')

        self.source.p4cmd('edit', file1)
        self.source.p4cmd('edit', file2)
        contents[0] = "file1"
        create_file(file1, "\n".join(contents) + "\n")
        contents2[5] = "file2"
        create_file(file2, "\n".join(contents2) + "\n")
        self.source.p4cmd('submit', '-d', 'file1 edited')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4.run_resolve('-am')
        self.source.p4cmd('submit', '-d', 'file1 -> file2 (merge)')

        filelog = self.source.p4.run_filelog('//depot/inside/file2')
        self.assertEqual("merge from", filelog[0].revisions[0].integrations[0].how)

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '3'
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(4, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("integrate", filelog[0].revisions[0].action)
        self.assertEqual("add", filelog[0].revisions[1].action)

    def testHistoricalRename(self):
        "Rename for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        contents = ["0"] * 10
        create_file(file1, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        self.source.p4cmd('edit', file1)
        contents[0] = "file1"
        self.source.p4cmd('submit', '-d', '2: file1 edit')

        chg = self.source.p4.fetch_change()
        chg._description = "Test desc"
        self.source.p4.save_change(chg)

        self.source.p4cmd('edit', file1)
        self.source.p4cmd('move', file1, file2)
        self.source.p4cmd('submit', '-d', '4: file1 renamed to file2')

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '3'
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(4, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(1, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        # self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("move/add", filelog[0].revisions[0].action)

        self.source.p4cmd('edit', file2)
        self.source.p4cmd('move', file2, file1)
        self.source.p4cmd('submit', '-d', '5: renamed back')

        self.run_P4Transfer()
        self.assertCounters(5, 3)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(0, len(filelog[0].revisions[0].integrations))
        self.assertEqual(2, len(filelog[0].revisions[1].integrations))
        # self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("move/delete", filelog[0].revisions[0].action)
        self.assertEqual("move/add", filelog[0].revisions[1].action)

        filelog = self.target.p4.run_filelog('//depot/import/file1')
        self.assertEqual(3, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual(1, len(filelog[0].revisions[2].integrations))
        # self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("move/add", filelog[0].revisions[0].action)

    def testHistoricalAddDelete(self):
        "Old style rename for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        contents = ["0"] * 10
        create_file(file1, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        self.source.p4cmd('integ', file1, file2)
        self.source.p4cmd('submit', '-d', '2: file1 -> file2')

        chg = self.source.p4.fetch_change()
        chg._description = "Test desc"
        self.source.p4.save_change(chg)

        self.source.p4cmd('integ', file2, file3)
        self.source.p4cmd('delete', file2)
        self.source.p4cmd('submit', '-d', '4: file2 delete/copy')

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '3'
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(4, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(0, len(filelog[0].revisions[0].integrations))
        self.assertEqual("delete", filelog[0].revisions[0].action)

        filelog = self.target.p4.run_filelog('//depot/import/file3')
        self.assertEqual(1, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual("branch", filelog[0].revisions[0].action)

    def testHistoricalRenameAdd(self):
        "Rename with subsequent add for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        contents = ["0"] * 10
        create_file(file1, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        self.source.p4cmd('edit', file1)
        contents[0] = "file1"
        self.source.p4cmd('submit', '-d', '2: file1 edit')

        chg = self.source.p4.fetch_change()
        chg._description = "Test desc"
        self.source.p4.save_change(chg)

        self.source.p4cmd('edit', file1)
        self.source.p4cmd('move', file1, file2)
        self.source.p4cmd('submit', '-d', '4: file1 renamed to file2')

        create_file(file3, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file3)
        self.source.p4cmd('submit', '-d', '5: add file3')

        self.source.p4cmd('edit', file3)
        self.source.p4cmd('move', file3, file1)
        self.source.p4cmd('submit', '-d', '6: rename 3->1')

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '3'
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(6, 4)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(1, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        # self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("move/add", filelog[0].revisions[0].action)

        filelog = self.target.p4.run_filelog('//depot/import/file3')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(0, len(filelog[0].revisions[0].integrations))
        # self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("move/delete", filelog[0].revisions[0].action)

        filelog = self.target.p4.run_filelog('//depot/import/file1')
        self.assertEqual(3, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual(1, len(filelog[0].revisions[2].integrations))
        # self.assertEqual(0, len(filelog[0].revisions[1].integrations))
        self.assertEqual("move/add", filelog[0].revisions[0].action)

    def testHistoricalStartMerge(self):
        "Merge for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        contents = ["0"] * 10
        contents2 = contents[:]
        create_file(file1, "\n".join(contents) + "\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        file2 = os.path.join(inside, "file2")
        self.source.p4cmd('integrate', file1, file2)
        self.source.p4cmd('submit', '-d', '2: file1 -> file2')

        self.source.p4cmd('edit', file1)
        self.source.p4cmd('edit', file2)
        contents[0] = "file1"
        create_file(file1, "\n".join(contents) + "\n")
        contents2[5] = "file2"
        create_file(file2, "\n".join(contents2) + "\n")
        self.source.p4cmd('submit', '-d', '3: file1&2 edited')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4.run_resolve('-am')
        self.source.p4cmd('submit', '-d', '4: file1 -> file2 (merge)')

        filelog = self.source.p4.run_filelog('//depot/inside/file2')
        self.assertEqual("merge from", filelog[0].revisions[0].integrations[0].how)

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '4'
        self.createConfigFile(options=config)

        self.run_P4Transfer()
        self.assertCounters(4, 1)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(1, len(filelog[0].revisions))
        self.assertEqual(0, len(filelog[0].revisions[0].integrations))
        self.assertEqual("add", filelog[0].revisions[0].action)

        self.source.p4cmd('integrate', file2, file1)
        self.source.p4.run_resolve('-am')
        self.source.p4cmd('submit', '-d', '5: file2 -> file1 (merge)')

        self.run_P4Transfer()
        self.assertCounters(5, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file1')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual("integrate", filelog[0].revisions[0].action)

    def testHistoricalStartSimple3(self):
        "Simple integration options for historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        create_file(file1, "Test content\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        file2 = os.path.join(inside, "file2")
        self.source.p4cmd('integrate', file1, file2)
        self.source.p4cmd('submit', '-d', '2: file1 -> file2')

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '3'
        self.createConfigFile(options=config)

        self.source.p4cmd('edit', file1)
        append_to_file(file1, "Rev2 chg3\n")
        self.source.p4cmd('submit', '-d', '3: file1 edited')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', '4: file1 -> file2 (copy rev2)')

        self.run_P4Transfer()
        self.assertCounters(4, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual("integrate", filelog[0].revisions[0].action)
        self.assertEqual("add", filelog[0].revisions[1].action)

        self.logger.debug("========================================== Incremental change")
        # Now make 2 changes and integrate them one at a time.
        self.source.p4cmd('edit', file1)
        append_to_file(file1, "Rev3 chg5\n")
        self.source.p4cmd('submit', '-d', '5: file1 edited')

        self.source.p4cmd('edit', file1)
        append_to_file(file1, "Rev4 chg6\n")
        self.source.p4cmd('submit', '-d', '6: file1 edited')

        self.source.p4cmd('integrate', file1 + "#3", file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', '7: file1 -> file2 (copy rev3)')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', '8: file1 -> file2 (copy rev4)')

        self.run_P4Transfer()
        self.assertCounters(8, 6)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        # self.logger.debug(filelog)
        self.assertEqual(4, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[1].integrations))
        self.assertEqual("copy from", filelog[0].revisions[0].integrations[0].how)
        self.assertEqual("copy from", filelog[0].revisions[1].integrations[0].how)
        self.assertEqual("copy from", filelog[0].revisions[2].integrations[0].how)
        self.assertEqual("add", filelog[0].revisions[3].action)

    def testHistoricalSubsequentMerge(self):
        "Integration after historical start mode transfer"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        create_file(file1, "Test content\n")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '1: file1 added')

        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '2'
        self.createConfigFile(options=config)

        create_file(file2, "Test content2\n")
        self.source.p4cmd('add', file2)
        self.source.p4cmd('submit', '-d', '2: file2 added')

        self.source.p4cmd('integrate', file2, file3)
        self.source.p4cmd('submit', '-d', '3: file2 -> file3')

        self.source.p4cmd('edit', file2)
        append_to_file(file2, "More\n")
        self.source.p4cmd('submit', '-d', '4: file2 edited')

        self.source.p4cmd('integrate', file2, file3)
        self.source.p4cmd('resolve', "-as")
        self.source.p4cmd('submit', '-d', '5: file2 -> file3')

        self.run_P4Transfer()
        self.assertCounters(5, 4)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual("edit", filelog[0].revisions[0].action)
        self.assertEqual("add", filelog[0].revisions[1].action)

        filelog = self.target.p4.run_filelog('//depot/import/file3')
        self.assertEqual(2, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual(1, len(filelog[0].revisions[1].integrations))
        self.assertEqual("copy from", filelog[0].revisions[0].integrations[0].how)
        self.assertEqual("branch from", filelog[0].revisions[1].integrations[0].how)

    def testHistoricalTooManyTargetRevs(self):
        "Historical integration where target has extra revs so wrong one is picked"
        self.setupTransfer()

        _ = self.source.p4.fetch_change()  # Create change no
        # In HistoricalStart mode we don't start from first change
        config = self.getDefaultOptions()
        config['historical_start_change'] = '2'
        self.createConfigFile(options=config)
        self.setTargetCounter('1')

        inside = localDirectory(self.source.client_root, "inside")
        timport = localDirectory(self.target.client_root, "import")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        tfile1 = os.path.join(timport, "file1")
        tfile2 = os.path.join(timport, "file2")

        create_file(file1, "Test content\n")
        create_file(tfile1, "Test targ content\n")

        # Add pre-existing target files
        self.target.p4cmd('add', tfile1)
        self.target.p4cmd('submit', '-d', '1: file1 added')

        self.target.p4cmd('integrate', tfile1, tfile2)
        self.target.p4cmd('submit', '-d', '2: file1 -> file2')

        self.target.p4cmd('delete', tfile1, tfile2)
        self.target.p4cmd('submit', '-d', '3: delete both')

        # Now source files we want to replicate
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', '2: file1 added')

        self.run_P4Transfer()
        self.assertCounters(2, 4)

        self.target.p4cmd('obliterate', '-y', '@1,3')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4cmd('submit', '-d', '3: file1 -> file2')

        self.source.p4cmd('edit', file1)
        append_to_file(file1, "More again\n")
        self.source.p4cmd('submit', '-d', '4: file1 edited')

        self.source.p4cmd('integrate', file1, file2)
        self.source.p4cmd('resolve', "-as")
        self.source.p4cmd('submit', '-d', '5: file1 -> file2')

        self.run_P4Transfer()
        self.assertCounters(4, 6)

        filelog = self.target.p4.run_filelog('//depot/import/file2')
        self.assertEqual(1, len(filelog[0].revisions))
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual("branch", filelog[0].revisions[0].action)

    def testComplexIntegrate(self):
        "More complex integrations with various resolve options"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        content1 = """
        Line 1
        Line 2 - changed
        Line 3
        """

        create_file(inside_file1, """
        Line 1
        Line 2
        Line 3
        """)
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        # Prepare merge
        self.source.p4cmd('edit', inside_file1, inside_file2)
        create_file(inside_file1, content1)
        create_file(inside_file2, """
        Line 1
        Line 2
        Line 3 - changed
        """)
        self.source.p4cmd('submit', '-d', "Changed both contents")

        # Integrate with merge
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4.run_resolve('-am')
        self.source.p4cmd('submit', '-d', "Merged contents")

        contentMerged = self.source.p4.run_print(inside_file2)[1]

        sourceCounter = 4
        targetCounter = 4

        self.run_P4Transfer()
        self.assertCounters(sourceCounter, targetCounter)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.logger.debug('test:', filelog)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'merge from')
        self.assertEqual(self.target.p4.run_print('//depot/import/inside_file2')[1], contentMerged)

        # Prepare integrate with edit
        self.source.p4cmd('edit', inside_file1, inside_file2)
        create_file(inside_file1, content1)
        self.source.p4cmd('submit', '-d', "Created a conflict")

        # Integrate with edit
        self.source.p4cmd('integrate', inside_file1, inside_file2)

        class EditResolve(P4.Resolver):
            def resolve(self, mergeData):
                create_file(mergeData.result_path, """
        Line 1
        Line 2 - changed
        Line 3 - edited
        """)
                return 'ae'

        self.source.p4.run_resolve(resolver=EditResolve())
        self.source.p4cmd('submit', '-d', "Merge with edit")

        sourceCounter += 2
        targetCounter += 2

        self.run_P4Transfer()
        self.assertCounters(sourceCounter, targetCounter)

        # Prepare ignore
        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "For your eyes only")
        self.source.p4cmd('submit', '-d', "Edit source again")

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4.run_resolve('-ay')  # ignore
        self.source.p4cmd('submit', '-d', "Ignored change in inside_file1")

        sourceCounter += 2
        targetCounter += 2

        self.run_P4Transfer()
        self.assertCounters(sourceCounter, targetCounter)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'ignored')
        content = self.target.p4.run_print('-a', '//depot/import/inside_file2')
        self.assertEqual(content[1], content[3])

        # Prepare delete
        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', "Delete file 1")

        self.source.p4.run_merge(inside_file1, inside_file2)  # to trigger resolve
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', "Propagated delete")

        sourceCounter += 2
        targetCounter += 2

        self.run_P4Transfer()
        self.assertCounters(sourceCounter, targetCounter)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'delete from')

        # Prepare re-add
        create_file(inside_file1, content1)
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 re-added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', "inside_file2 re-added")

        sourceCounter += 2
        targetCounter += 2

        self.run_P4Transfer()
        self.assertCounters(sourceCounter, targetCounter)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'branch from')

    def testIntegSelective(self):
        "Make sure selective integrate performed appropriately"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        content_edit1 = """
        Line 1 - changed
        Line 2
        Line 3
        """
        content_edit2 = """
        Line 1 - changed
        Line 2
        Line 3 - changed
        """
        content_v3 = """
        Line 1
        Line 2
        Line 3 - changed
        """

        create_file(inside_file1, """
        Line 1
        Line 2
        Line 3
        """)
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        self.source.p4cmd('edit', inside_file1)
        create_file(inside_file1, content_edit1)
        self.source.p4cmd('submit', '-d', "Changed content")

        self.source.p4cmd('edit', inside_file1)
        create_file(inside_file1, content_edit2)
        self.source.p4cmd('submit', '-d', "Changed content2")

        # Integrate with merge
        self.source.p4cmd('integrate', "%s#3,3" % inside_file1, inside_file2)
        self.source.p4.run_resolve('-am')
        self.source.p4cmd('submit', '-d', "Selective propagate")

        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual('merge from', filelog[0].revisions[0].integrations[0].how)
        self.assertEqual(2, filelog[0].revisions[0].integrations[0].srev)
        self.assertEqual(3, filelog[0].revisions[0].integrations[0].erev)
        self.assertContentsEqual(content_v3, self.source.p4.run_print(inside_file2)[1])

        sourceCounter = 5
        targetCounter = 5

        self.run_P4Transfer()
        self.assertCounters(sourceCounter, targetCounter)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.logger.debug('test:', filelog)
        self.assertEqual('merge from', filelog[0].revisions[0].integrations[0].how, )
        self.assertEqual(2, filelog[0].revisions[0].integrations[0].srev)
        self.assertEqual(3, filelog[0].revisions[0].integrations[0].erev)
        self.assertContentsEqual(content_v3, self.target.p4.run_print('//depot/import/inside_file2')[1])

    def testMultipleOverlappingIntegrates(self):
        "More integrates which overlap"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")

        create_file(inside_file1, "test content\n")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "More content\n")
        self.source.p4cmd('submit', '-d', "Changed file1")

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        self.source.p4cmd('edit', inside_file1)
        create_file(inside_file1, "New content\n")
        self.source.p4cmd('submit', '-d', "Changed file1")

        self.source.p4cmd('edit', inside_file2)
        create_file(inside_file2, "More new content\n")
        self.source.p4cmd('submit', '-d', "Changed file2")

        self.source.p4cmd('integrate', inside_file1, inside_file2)

        class EditResolve(P4.Resolver):
            def resolve(self, mergeData):
                create_file(mergeData.result_path, "different contents\n")
                return 'ae'

        self.source.p4.run_resolve(resolver=EditResolve())
        self.source.p4cmd('integrate', '-f', inside_file1, inside_file2)
        self.source.p4.run_resolve('-ay')
        self.source.p4cmd('submit', '-d', "Merge with edit")

        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')
        self.assertEqual(filelog[0].revisions[0].integrations[1].how, 'ignored')

        self.run_P4Transfer()
        self.assertCounters(6, 6)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(1, len(filelog[0].revisions[0].integrations))
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')

    def testForcedIntegrate(self):
        "Integration requiring -f"
        self.setupTransfer()
        self.source.p4cmd("configure", "set", "dm.integ.engine=2")
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "more content")
        self.source.p4cmd('submit', '-d', 'added content')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2 again')

        # Backout the change which was integrated in, and then force integrate
        self.source.p4cmd('sync', "%s#1" % inside_file2)
        self.source.p4cmd('edit', inside_file2)
        self.source.p4cmd('sync', inside_file2)
        self.source.p4cmd('resolve', '-at', inside_file2)
        self.source.p4cmd('submit', '-d', 'backed out inside_file2')

        self.source.p4cmd('integrate', '-f', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'Force integrate')

        self.run_P4Transfer()
        self.assertCounters(6, 6)

    def testDirtyMerge(self):
        """A merge which is supposedly clean but in reality is a dirty one - this is possible when transferring with
        old servers"""
        self.setupTransfer()

        content = """
        Line 1
        Line 2
        Line 3
        """

        content2 = """
        Line 1
        Line 2
        Line 3 - changed
        """

        content3 = """
        Line 1
        Line 2 - changed
        Line 3 - edited
        """

        content4 = """
        Line 1
        Line 2 - changed
        Line 3 - edited and changed
        """

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, content)
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        # Prepare merge with edit
        self.source.p4cmd('edit', inside_file1, inside_file2)
        create_file(inside_file1, content2)
        create_file(inside_file2, content3)
        self.source.p4cmd('submit', '-d', "Changed both contents")

        self.source.p4cmd('integrate', inside_file1, inside_file2)

        class EditResolve(P4.Resolver):
            def resolve(self, mergeData):
                create_file(mergeData.result_path, content4)
                return 'ae'

        self.source.p4.run_resolve(resolver=EditResolve())
        self.source.p4cmd('submit', '-d', "Merge with edit")

        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')

        # Convert dirty merge to pretend clean merge.
        #
        # Dirty merge (fields 12/10)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 1 2 2 3 12 4
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 2 3 1 2 10 4
        #
        # Clean merge (fields 0/1)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_ file1@ 1 2 2 3 0 4
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 2 3 1 2 1 4
        jnl_rec = "@rv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 1 2 2 3 0 4\n" + \
            "@rv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 2 3 1 2 1 4\n"
        self.applyJournalPatch(jnl_rec)

        self.run_P4Transfer()
        self.assertCounters(4, 4)

    def testDodgyMerge(self):
        """Like testDirtyMerge but user has cherry picked and then hand edited - clean merge is different on disk"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file3 = os.path.join(inside, "inside_file3")
        inside_file5 = os.path.join(inside, "inside_file5")

        create_file(inside_file1, dedent("""
        Line 1
        Line 2
        Line 3
        """))
        create_file(inside_file3, dedent("""
        Line 1 $Id$
        Line 2
        Line 3
        """))
        create_file(inside_file5, dedent("""
        Line 1
        Line 2
        Line 3
        """))
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('add', '-t', 'ktext', inside_file3)
        self.source.p4cmd('add', '-t', 'ktext', inside_file5)
        self.source.p4cmd('submit', '-d', 'inside_files added')

        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file4 = os.path.join(inside, "inside_file4")
        inside_file6 = os.path.join(inside, "inside_file6")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('integrate', inside_file3, inside_file4)
        self.source.p4cmd('integrate', inside_file5, inside_file6)
        self.source.p4cmd('submit', '-d', 'inside_files integrated')

        self.source.p4cmd('edit', inside_file1, inside_file3, inside_file5)
        create_file(inside_file1, dedent("""
        Line 1 - changed file1
        Line 2
        Line 3
        """))
        create_file(inside_file3, dedent("""
        Line 1 - $Id$ changed file3
        Line 2
        Line 3
        """))
        create_file(inside_file5, dedent("""
        Line 1 - changed file5
        Line 2
        Line 3
        """))
        self.source.p4cmd('submit', '-d', "Changed inside_files")

        self.source.p4cmd('edit', inside_file1, inside_file3, inside_file5)
        create_file(inside_file1, dedent("""
        Line 1 - changed file1
        Line 2
        Line 3 - changed file1
        """))
        create_file(inside_file3, dedent("""
        Line 1 - $Id$ changed file3
        Line 2
        Line 3 - changed file3
        """))
        create_file(inside_file5, dedent("""
        Line 1 - changed file5
        Line 2
        Line 3 - changed file5
        """))
        self.source.p4cmd('submit', '-d', "Changed inside_files")

        self.source.p4cmd('edit', inside_file2, inside_file4, inside_file6)
        create_file(inside_file2, dedent("""
        Line 1
        Line 2 - changed file2
        Line 3
        """))
        create_file(inside_file4, dedent("""
        Line 1
        Line 2 - changed file4
        Line 3
        """))
        create_file(inside_file6, dedent("""
        Line 1
        Line 2 - changed file6
        Line 3
        """))
        self.source.p4cmd('submit', '-d', "Changed inside_files")

        class EditResolve(P4.Resolver):
            def __init__(self, content):
                self.content = content

            def resolve(self, mergeData):
                create_file(mergeData.result_path, self.content)
                return 'ae'

        # Merge with edit - but cherry picked
        self.source.p4cmd('integrate', "%s#3,3" % inside_file1, inside_file2)
        self.source.p4.run_resolve(resolver=EditResolve(dedent("""
        Line 1 - edited
        Line 2 - changed file2
        Line 3 - changed file1
        """)))
        self.source.p4cmd('integrate', "%s#3,3" % inside_file3, inside_file4)
        self.source.p4.run_resolve(resolver=EditResolve(dedent("""
        Line 1 - $Id$ changed file3
        Line 2 - changed file4
        Line 3 - changed file3
        """)))
        self.source.p4cmd('integrate', "%s#3,3" % inside_file5, inside_file6)
        self.source.p4.run_resolve(resolver=EditResolve(dedent("""
        Line 1 - edited
        Line 2 - changed file6
        Line 3 - changed file5
        """)))
        self.source.p4cmd('submit', '-d', "Merge with edit")

        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')
        self.logger.debug("print:", self.source.p4.run_print(inside_file2))

        # Convert dirty merge to clean merge
        #
        # Dirty merge (fields 12/10)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 2 3 2 3 12 6
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 2 3 2 3 10 6
        #
        # Clean merge (fields 0/1)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_ file1@ 1 2 2 3 0 4
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 2 3 1 2 1 4
        jnl_rec = "@rv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 2 3 2 3 0 6\n" + \
            "@pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 2 3 2 3 1 6\n" + \
            "@rv@ 0 @db.integed@ @//depot/inside/inside_file4@ @//depot/inside/inside_file3@ 2 3 2 3 0 6\n" + \
            "@pv@ 0 @db.integed@ @//depot/inside/inside_file3@ @//depot/inside/inside_file4@ 2 3 2 3 1 6\n" + \
            "@rv@ 0 @db.integed@ @//depot/inside/inside_file6@ @//depot/inside/inside_file5@ 2 3 2 3 0 6\n" + \
            "@pv@ 0 @db.integed@ @//depot/inside/inside_file5@ @//depot/inside/inside_file6@ 2 3 2 3 1 6\n"
        self.applyJournalPatch(jnl_rec)

        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'merge from')
        self.logger.debug("print:", self.source.p4.run_print(inside_file2))

        self.run_P4Transfer()
        self.assertCounters(6, 6)
        self.logger.debug("print:", self.target.p4.run_print("//depot/import/inside_file2"))

        filelog = self.target.p4.run_filelog("//depot/import/inside_file2")
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')

        filelog = self.target.p4.run_filelog("//depot/import/inside_file4")
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')

        filelog = self.target.p4.run_filelog("//depot/import/inside_file6")
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'edit from')

        content = self.target.p4.run_print('//depot/import/inside_file4')[1]
        if python3:
            content = content.decode()
        lines = content.split("\n")
        self.assertEqual(lines[1], 'Line 1 - $Id: //depot/import/inside_file4#3 $ changed file3')
        self.assertEqual(lines[2], 'Line 2 - changed file4')
        self.assertEqual(lines[3], 'Line 3 - changed file3')

        content = self.target.p4.run_print('//depot/import/inside_file6')[1]
        if python3:
            content = content.decode()
        lines = content.split("\n")
        self.assertEqual(lines[1], 'Line 1 - edited')
        self.assertEqual(lines[2], 'Line 2 - changed file6')
        self.assertEqual(lines[3], 'Line 3 - changed file5')

    def testRemoteIntegs(self):
        """An integrate from a remote depot - gives the 'import' action """
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, "Some content")
        self.source.p4cmd("add", inside_file1)
        self.source.p4cmd("submit", '-d', 'inside_file1 added')

        filelog = self.source.p4.run_filelog(inside_file1)
        self.assertEqual(filelog[0].revisions[0].action, 'add')

        recs = self.dumpDBFiles("db.rev,db.revcx,db.revhx")
        self.logger.debug(recs)
        # '@pv@ 9 @db.rev@ @//depot/inside/inside_file1@ 1 0 0 1 1420649505 1420649505 581AB2D8...69C623BDEF83 13 0 0 @//depot/inside/inside_file1@ @1.1@ 0 ',
        # @pv@ 0 @db.revcx@ 1 @//depot/inside/inside_file1@ 1 0 ',
        # '@pv@ 9 @db.revhx@ @//depot/inside/inside_file1@ 1 0 0 1 1420649505 1420649505 581AB2D8...69C623BDEF83 13 0 0 @//depot/inside/inside_file1@ @1.1@ 0 '
        recs[0] = recs[0].replace("@ 1 0 0 1 ", "@ 1 0 5 1 ")
        recs[1] = recs[1].replace("@ 1 0", "@ 1 5")
        recs[2] = recs[2].replace("@ 1 0 0 1 ", "@ 1 0 5 1 ")
        recs[0] = recs[0].replace("@pv@", "@rv@")
        recs[1] = recs[1].replace("@pv@", "@rv@")
        recs[2] = recs[2].replace("@pv@", "@rv@")

        self.applyJournalPatch("\n".join(recs))

        filelog = self.source.p4.run_filelog(inside_file1)
        self.assertEqual(filelog[0].revisions[0].action, 'import')

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'add')

    def testDirtyBranch(self):
        """A copy which is supposedly clean but in reality has been edited"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, "Some content")
        self.source.p4cmd("add", inside_file1)
        self.source.p4cmd("submit", '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd("integrate", inside_file1, inside_file2)
        self.source.p4cmd("edit", inside_file2)
        append_to_file(inside_file2, "New content")
        self.source.p4cmd("submit", '-d', 'inside_file1 -> inside_file2 with edit')

        filelog = self.source.p4.run_filelog(inside_file1)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'add into')

        # Needs to be created via journal patch
        #
        # Branch as edit (fields 2/11)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 0 1 0 1 2 2
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 0 1 0 1 11 2
        #
        # Clean branch (fields 2/3)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 0 1 0 1 2 2
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 0 1 0 1 3 2
        jnl_rec = "@rv@ 0 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file1@ 0 1 0 1 2 2\n" + \
            "@rv@ 0 @db.integed@ @//depot/inside/inside_file1@ @//depot/inside/inside_file2@ 0 1 0 1 3 2\n"
        self.applyJournalPatch(jnl_rec)

        filelog = self.source.p4.run_filelog(inside_file1)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'branch into')
        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'branch from')

        self.run_P4Transfer()
        self.assertCounters(2, 2)

    def testMultipleSimpleIntegrate(self):
        "Multiple integrations transferred in one go"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, "Some content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file1 -> inside_file2')

        file3 = os.path.join(inside, "file3")
        self.source.p4cmd('integrate', inside_file2, file3)
        self.source.p4cmd('submit', '-d', 'inside_file2 -> File3')

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog1 = self.target.p4.run_filelog('//depot/import/inside_file1')
        filelog2 = self.target.p4.run_filelog('//depot/import/inside_file2')
        filelog3 = self.target.p4.run_filelog('//depot/import/file3')
        self.logger.debug(filelog1)
        self.logger.debug(filelog2)
        self.logger.debug(filelog3)

        self.assertEqual(len(filelog1[0].revisions), 1)
        self.assertEqual(len(filelog2[0].revisions), 1)
        self.assertEqual(len(filelog3[0].revisions), 1)

        self.assertEqual(len(filelog1[0].revisions[0].integrations), 1)
        self.assertEqual(len(filelog2[0].revisions[0].integrations), 2)
        self.assertEqual(len(filelog3[0].revisions[0].integrations), 1)

        self.assertEqual(filelog1[0].revisions[0].integrations[0].how, 'branch into')
        self.assertEqual(filelog2[0].revisions[0].integrations[0].how, 'branch into')
        self.assertEqual(filelog2[0].revisions[0].integrations[1].how, 'branch from')
        self.assertEqual(filelog3[0].revisions[0].integrations[0].how, 'branch from')

    def testMultipleIntegrates(self):
        """Test for more than one integration into same target revision"""
        self.setupTransfer()
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")
        self.source.p4cmd("configure", "set", "dm.integ.engine=2")

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "more stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nyet more stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited again')

        self.source.p4cmd('integrate', "%s#2" % inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-as')

        self.source.p4cmd('integrate', "%s#3,3" % inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-ay')   # Ignore

        self.source.p4cmd('submit', '-d', 'integrated twice separately into file2')

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "ignored")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "copy from")

    def testIgnoreFiles(self):
        "Test when specifying files to ignore"
        self.setupTransfer()

        config = self.getDefaultOptions()
        config['ignore_files'] = ['file2$']
        self.createConfigFile(options=config)

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, 'file1')
        file2 = os.path.join(inside, 'file2')
        create_file(file1, "Test content")
        create_file(file2, "Test content")
        self.source.p4cmd('add', file1, file2)
        self.source.p4cmd('submit', '-d', "Added files")

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        changes = self.target.p4cmd('changes')
        self.assertEqual(1, len(changes))
        files = self.target.p4.run_files('//depot/import/...')
        self.assertEqual(1, len(files))
        self.assertEqual(files[0]['depotFile'], '//depot/import/file1')

    def testExclusionsInClientMap(self):
        "Test exclusion lines in client views work"
        self.setupTransfer()

        config = self.getDefaultOptions()
        config['views'] = [{'src': '//depot/inside/...',
                           'targ': '//depot/import/...'},
                           {'src': '-//depot/inside/sub/...',
                           'targ': '//depot/import/sub/...'}]
        self.createConfigFile(options=config)

        inside = localDirectory(self.source.client_root, "inside")
        subdir = localDirectory(self.source.client_root, "inside", "sub")

        file1 = os.path.join(inside, 'file1')
        file2 = os.path.join(subdir, 'file2')
        create_file(file1, "Test content")
        create_file(file2, "Test content")
        self.source.p4cmd('add', file1, file2)
        self.source.p4cmd('submit', '-d', "Added files")

        self.run_P4Transfer()
        self.assertCounters(1, 1)

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 1, "Not exactly one change on target")
        filelog = self.target.p4.run_filelog('//depot/import/...')
        self.assertEqual(len(filelog), 1, "Not exactly one file on target")
        self.assertEqual(filelog[0].revisions[0].action, "add")

        expView = ['//depot/inside/... //%s/depot/inside/...' % TRANSFER_CLIENT,
                   '-//depot/inside/sub/... //%s/depot/inside/sub/...' % TRANSFER_CLIENT]
        client = self.source.p4.fetch_client(TRANSFER_CLIENT)
        self.assertEqual(expView, client._view)

        expView = ['//depot/import/... //%s/depot/inside/...' % TRANSFER_CLIENT,
                   '-//depot/import/sub/... //%s/depot/inside/sub/...' % TRANSFER_CLIENT]
        client = self.target.p4.fetch_client(TRANSFER_CLIENT)
        self.assertEqual(expView, client._view)

    def testInsideOutside(self):
        "Test integrations between the inside<->outside where only one element is thus transferred"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")

        # add from outside, integrate in
        outside_file = os.path.join(outside, 'outside_file')
        create_file(outside_file, "Some content")
        self.source.p4cmd('add', outside_file)
        self.source.p4cmd('submit', '-d', "Outside outside_file")

        inside_file2 = os.path.join(inside, 'inside_file2')
        self.source.p4cmd('integrate', outside_file, inside_file2)
        self.source.p4cmd('submit', '-d', "Integrated from outside to inside")

        self.run_P4Transfer()
        self.assertCounters(2, 1)

        changes = self.target.p4cmd('changes', )
        self.assertEqual(len(changes), 1, "Not exactly one change on target")
        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].action, "add")

        # edit from outside, integrated in
        self.source.p4cmd('edit', outside_file)
        append_to_file(outside_file, "More content")
        self.source.p4cmd('submit', '-d', "Outside outside_file edited")

        self.run_P4Transfer()
        self.assertCounters(2, 1)   # counters will not move, no change within the client workspace's scope

        self.source.p4cmd('integrate', outside_file, inside_file2)
        self.source.p4.run_resolve('-at')
        self.source.p4cmd('submit', '-d', "Copied outside_file -> inside_file2")

        self.run_P4Transfer()
        self.assertCounters(4, 2)

        changes = self.target.p4cmd('changes', )
        self.assertEqual(len(changes), 2)
        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].action, "edit")

        # delete from outside, integrate in
        self.source.p4cmd('delete', outside_file)
        self.source.p4cmd('submit', '-d', "outside_file deleted")

        self.source.p4cmd('integrate', outside_file, inside_file2)
        self.source.p4cmd('submit', '-d', "inside_file2 deleted from outside_file")

        self.run_P4Transfer()
        self.assertCounters(6, 3)

        changes = self.target.p4cmd('changes', )
        self.assertEqual(len(changes), 3, "Not exactly three changes on target")
        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].action, "delete")

        # Add to both inside and outside in the same changelist - check only inside transferred
        create_file(inside_file2, "Different content")
        create_file(outside_file, "Different content")
        self.source.p4cmd('add', inside_file2, outside_file)
        self.source.p4cmd('submit', '-d', "adding inside and outside")

        self.run_P4Transfer()
        self.assertCounters(7, 4)

        change = self.target.p4.run_describe('4')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/inside_file2')

    def testOutsideInsideDirtyCopy(self):
        "Test integrations between the inside<->outside where copy is not actually clean"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")

        # Add and delete inside file
        inside_file = os.path.join(inside, 'inside_file')
        create_file(inside_file, "Inside content")
        self.source.p4cmd('add', inside_file)
        self.source.p4cmd('submit', '-d', "Added inside_file")
        self.source.p4cmd('delete', inside_file)
        self.source.p4cmd('submit', '-d', "Deleted inside_file")

        # add from outside, integrate in
        outside_file = os.path.join(outside, 'outside_file')
        create_file(outside_file, "Outside content")
        self.source.p4cmd('add', outside_file)
        self.source.p4cmd('submit', '-d', "Outside outside_file")

        self.source.p4cmd('integrate', outside_file, inside_file)
        self.source.p4cmd('edit', inside_file)
        append_to_file(inside_file, "extra stuff")
        self.source.p4cmd('submit', '-d', "Integrated from outside to inside")

        filelog = self.source.p4.run_filelog(outside_file)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'add into')

        # Branch as edit (fields 2/11)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file@ @//depot/outside/outside_file@ 0 1 2 3 2 4
        # @pv@ 0 @db.integed@ @//depot/outside/outside_file@ @//depot/inside/inside_file@ 2 3 0 1 11 4
        #
        # Clean branch (fields 2/3)
        # @pv@ 0 @db.integed@ @//depot/inside/inside_file@ @//depot/outside/outside_file@ 0 1 2 3 2 4
        # @pv@ 0 @db.integed@ @//depot/outside/outside_file@ @//depot/inside/inside_file@ 2 3 0 1 3 4
        jnl_rec = "@rv@ 0 @db.integed@ @//depot/inside/inside_file@ @//depot/outside/outside_file@ 0 1 2 3 2 4\n" + \
            "@rv@ 0 @db.integed@ @//depot/outside/outside_file@ @//depot/inside/inside_file@ 2 3 0 1 3 4\n"
        self.applyJournalPatch(jnl_rec)

        filelog = self.source.p4.run_filelog(inside_file)
        self.assertEqual(len(filelog[0].revisions[0].integrations), 1)
        self.assertEqual(filelog[0].revisions[0].integrations[0].how, 'branch from')

        self.run_P4Transfer()
        self.assertCounters(4, 3)

    def testFailedSubmit(self):
        """Test what happens if submits fail, e.g. due to trigger"""
        self.setupTransfer()

        protect = self.source.p4.fetch_protect()
        self.logger.debug('protect:', protect)
        self.logger.debug(self.target.p4.save_protect(protect))
        self.target.p4cmd('admin', 'restart')
        self.target.p4.disconnect()
        self.target.p4.connect()

        triggers = self.target.p4.fetch_triggers()
        triggers['Triggers'] = ['test-trigger change-submit //depot/... "fail No submits allowed at this time"']
        self.target.p4.save_triggers(triggers)

        self.target.p4.disconnect()
        self.target.p4.connect()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file = os.path.join(inside, 'inside_file')
        create_file(inside_file, "Some content")
        self.source.p4cmd('add', inside_file)
        self.source.p4cmd('submit', '-d', "adding inside, hidden")

        result = self.run_P4Transfer()
        self.assertEqual(result, 1)

        self.assertCounters(0, 1)

    @unittest.skip("Not properly working test yet - issues around fetching protect...")
    def testHiddenFiles(self):
        """Test for adding and integrating from hidden files - not visible to transfer user"""
        self.setupTransfer()
        inside = localDirectory(self.source.client_root, "inside")

        inside_file = os.path.join(inside, 'inside_file')
        create_file(inside_file, "Some content")
        hidden_file = os.path.join(inside, 'hidden_file')
        create_file(hidden_file, "Some content")
        self.source.p4cmd('add', inside_file, hidden_file)
        self.source.p4cmd('submit', '-d', "adding inside, hidden")

        # Now change permissions

        self.source.p4cmd('edit', inside_file, hidden_file)
        append_to_file(inside_file, 'More content')
        append_to_file(hidden_file, 'More content')
        self.source.p4cmd('submit', '-d', "changing inside, hidden")

        p4superuser = 'p4newadmin'
        self.source.p4.user = p4superuser
        protect = self.source.p4.fetch_protect()
        protect['Protections'].append("write user %s * -//depot/...hidden_file" % P4USER)
        self.source.p4.save_protect(protect)
        self.logger.debug("protect:", self.source.p4.run_protect("-o"))

        self.source.p4.user = P4USER
        self.source.p4.disconnect()
        self.source.p4.connect()
        p = self.source.p4.run_protects('//depot/...')
        self.logger.debug('protects:', p)

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        change = self.target.p4.run_describe('1')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/inside_file')

        change = self.target.p4.run_describe('2')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/inside_file')

        # Edit and integrate in
        self.source.p4.user = p4superuser
        self.source.p4cmd('edit', hidden_file)
        append_to_file(hidden_file, 'Yet More content')
        self.source.p4cmd('submit', '-d', "Edited")

        self.source.p4.user = P4USER
        self.run_P4Transfer()
        self.assertCounters(2, 2)

        self.source.p4.user = p4superuser
        inside_file2 = os.path.join(inside, 'inside_file2')
        self.source.p4cmd('integrate', hidden_file, inside_file2)
        self.source.p4cmd('submit', '-d', "Copied outside_file -> inside_file2")
        self.logger.debug(self.source.p4.run_print(inside_file2))

        self.source.p4.user = P4USER
        self.run_P4Transfer()
        self.assertCounters(4, 3)

        change = self.target.p4.run_describe('3')[0]
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/inside_file2')
        self.assertEqual(change['action'][0], 'branch')

    def testIntegDt(self):
        """Test for integ -Dt being required - only necessary with older integ.engine"""
        self.setupTransfer()
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")
        self.source.p4cmd("configure", "set", "dm.integ.engine=2")

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 deleted')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 deleted')

        create_file(inside_file1, "Test content again")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.run_P4Transfer()
        self.assertCounters(6, 6)

    def testIntegDeleteForce(self):
        """Test for forced integrate of a delete being required"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'file added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'file2 added')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 deleted')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 deleted')

        create_file(inside_file2, "Test content again")
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.source.p4cmd('integrate', '-f', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-at', inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.run_P4Transfer()
        self.assertCounters(6, 6)

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 1)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "delete from")

    def testIntegMultipleToOne(self):
        """Test for integrating multiple versions into single target."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nmore stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nYet more stuff")
        self.source.p4cmd('submit', '-d', 'file edited again')

        class EditResolve(P4.Resolver):
            def resolve(self, mergeData):
                create_file(mergeData.result_path, "new contents\nsome more")
                return 'ae'

        self.source.p4cmd('integrate', "%s#1" % inside_file1, inside_file2)
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('integrate', "%s#2,2" % inside_file1, inside_file2)
        self.source.p4cmd('edit', inside_file2)
        self.source.p4.run_resolve(resolver=EditResolve())
        self.source.p4cmd('submit', '-d', 'inside_file2 added with multiple integrates')

        # Separate test - 3 into 1
        self.source.p4cmd('integrate', "%s#1" % inside_file1, inside_file3)
        self.source.p4cmd('integrate', "%s#2,2" % inside_file1, inside_file3)
        self.source.p4cmd('edit', inside_file3)
        self.source.p4cmd('resolve', '-am')
        self.source.p4cmd('integrate', "%s#3,3" % inside_file1, inside_file3)
        self.source.p4cmd('resolve', "-as")
        self.source.p4cmd('submit', '-d', 'inside_file3 added with multiple integrates')

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 3)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "merge from")
        self.assertEqual(filelog.revisions[0].integrations[2].how, "add from")

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "edit from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "add from")

        filelog = self.target.p4.run_filelog('//depot/import/inside_file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 3)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "ignored")
        self.assertEqual(filelog.revisions[0].integrations[2].how, "ignored")

    # Only works for p4d.21.1 or later
    def testIntegAndRenamePrevious(self):
        """Test for copying while renaming older file."""
        self.setupTransfer()

        # $ p4 describe -s 19027676 | grep Square.png
        # ... //UE5/RES/Tut/Square.png#2 branch
        # ... //UE5/RES/GuidedTut/Square.png#1 move/add

        # //UE5/Main/Tut/Square.png
        # ... #1 change 13149436 branch on 2020/05/04 by Ben.Marsh@Ben.Marsh_T3245_UE5 (binary+l) 'Merging from //UE4/Private-Star'
        # ... ... moved into //UE5/Main/GuidedTut/Square.png#1
        # ... ... branch into //UE5/RES/Tut/Square.png#1

        # $ p4 filelog -m2 //UE5/RES/Tut/Square.png#2
        # //UE5/RES/Tut/Square.png
        # ... #2 change 19027676 branch on 2022/02/16 by Marc.Audy@Marc.Audy_UE5_RES (binary+l) 'Update RES f'
        # ... ... branch from //UE5/Main/Tut/Square.png#3
        # ... #1 change 13667922 branch on 2020/06/11 by Marc.Audy@Marc.Audy_Z2487 (binary+l) 'Initial branch of files from Ma'
        # ... ... branch from //UE5/Main/Tut/Square.png#1
        # ... ... moved into //UE5/RES/GuidedTut/Square.png#1

        # $ p4 filelog -m1 //UE5/RES/GuidedTut/Square.png#1
        # //UE5/RES/GuidedTut/Square.png
        # ... #1 change 19027676 move/add on 2022/02/16 by Marc.Audy@Marc.Audy_UE5_RES (binary+l) 'Update RES f'
        # ... ... copy from //UE5/Main/GuidedTut/Square.png#1
        # ... ... moved from //UE5/RES/Tut/Square.png#1

        # add A1
        # branch A1 B1
        # branch A1 C1
        # move A1 A2
        # add A1
        # copy A... B...

        inside = localDirectory(self.source.client_root, "inside")
        mfile1 = os.path.join(inside, "main", "file1")
        mfile2 = os.path.join(inside, "main", "file2")
        rfile1 = os.path.join(inside, "rel", "file1")

        create_file(mfile1, "Test content")
        self.source.p4cmd('add', mfile1)
        self.source.p4cmd('submit', '-d', 'mfile1 added')

        self.source.p4cmd('integ', mfile1, rfile1)
        self.source.p4cmd('submit', '-d', 'mfile1 and rfile1 added')

        self.source.p4cmd('edit', mfile1)
        self.source.p4cmd('move', mfile1, mfile2)
        self.source.p4cmd('submit', '-d', 'mfile2 added')

        create_file(mfile1, "Test content2")
        self.source.p4cmd('add', mfile1)
        self.source.p4cmd('submit', '-d', 'mfile1 added back')

        self.source.p4cmd('copy', "//depot/inside/main/...", "//depot/inside/rel/...")
        self.source.p4cmd('submit', '-d', 'mfile1 added back')

        filelogs = self.source.p4.run_filelog("@=5")
        self.logger.debug(filelogs)
        self.assertEqual(3, len(filelogs))
        self.assertEqual(1, len(filelogs[0].revisions[0].integrations))
        self.assertEqual("branch from", filelogs[0].revisions[0].integrations[0].how)
        self.assertEqual(2, len(filelogs[1].revisions[0].integrations))
        self.assertEqual("copy from", filelogs[1].revisions[0].integrations[0].how)
        self.assertEqual("moved from", filelogs[1].revisions[0].integrations[1].how)

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelogs = self.target.p4.run_filelog("@=5")
        self.logger.debug(filelogs)
        self.assertEqual(3, len(filelogs))
        self.assertEqual(1, len(filelogs[0].revisions[0].integrations))
        self.assertEqual("branch from", filelogs[0].revisions[0].integrations[0].how)
        self.assertEqual(2, len(filelogs[1].revisions[0].integrations))
        self.assertEqual("copy from", filelogs[1].revisions[0].integrations[0].how)
        self.assertEqual("moved from", filelogs[1].revisions[0].integrations[1].how)

    def testIntegCopyAndRename(self):
        """Test for integrating a copy and move into single target."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        create_file(file1, "Test content")
        create_file(file2, "Test content2")
        self.source.p4cmd('add', file1, file2)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', file2)
        self.source.p4cmd('move', file2, file3)
        self.source.p4cmd('integrate', file1, file3)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'file3 added')

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "moved from")

        # We can't move onto
        recs = self.dumpDBFiles("db.integed")
        self.logger.debug(recs)
        # @pv@ 0 @db.integed@ @//stream/main/file1@ @//stream/main/file3@ 0 1 0 1 10 5
        # @pv@ 0 @db.integed@ @//stream/main/file2@ @//stream/main/file3@ 0 1 0 1 15 5
        # @pv@ 0 @db.integed@ @//stream/main/file3@ @//stream/main/file1@ 0 1 0 1 4 5
        # @pv@ 0 @db.integed@ @//stream/main/file3@ @//stream/main/file2@ 0 1 0 1 14 5

        # Convert copy -> branch - can't be done through front end

        newrecs = []
        for rec in recs:
            if "@db.integed@ @//depot/inside/file3@ @//depot/inside/file1@" in rec:
                rec = rec.replace("@ 0 1 0 1 4", "@ 0 1 0 1 2")     # 4->2
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.integed@ @//depot/inside/file1@ @//depot/inside/file3@" in rec:
                rec = rec.replace("@ 0 1 0 1 10", "@ 0 1 0 1 5")     # 10->5
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
        self.logger.debug("Newrecs:", "\n".join(newrecs))
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "branch from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "moved from")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(2, len(filelog.revisions[0].integrations))
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "moved from")

    def testIntegCopyAndRenameFromOutside(self):
        """Test for integrating a copy and move into single target with integ coming from outside."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")
        file1 = os.path.join(outside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        create_file(file1, "Test content")
        create_file(file2, "Test content2")
        self.source.p4cmd('add', file1, file2)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', file2)
        self.source.p4cmd('move', file2, file3)
        self.source.p4cmd('integrate', file1, file3)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'file3 added')

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "moved from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "copy from")

        # We can't move onto
        recs = self.dumpDBFiles("db.integed")
        self.logger.debug(recs)
        # @pv@ 0 @db.integed@ @//stream/main/file1@ @//stream/main/file3@ 0 1 0 1 10 5
        # @pv@ 0 @db.integed@ @//stream/main/file2@ @//stream/main/file3@ 0 1 0 1 15 5
        # @pv@ 0 @db.integed@ @//stream/main/file3@ @//stream/main/file1@ 0 1 0 1 4 5
        # @pv@ 0 @db.integed@ @//stream/main/file3@ @//stream/main/file2@ 0 1 0 1 14 5

        # Convert copy -> branch - can't be done through front end

        newrecs = []
        for rec in recs:
            if "@db.integed@ @//depot/inside/file3@ @//depot/outside/file1@" in rec:
                rec = rec.replace("@ 0 1 0 1 4", "@ 0 1 0 1 2")     # 4->2
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.integed@ @//depot/outside/file1@ @//depot/inside/file3@" in rec:
                rec = rec.replace("@ 0 1 0 1 10", "@ 0 1 0 1 5")     # 10->5
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
        self.logger.debug("Newrecs:", "\n".join(newrecs))
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "moved from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "branch from")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(1, len(filelog.revisions[0].integrations))
        self.assertEqual(filelog.revisions[0].integrations[0].how, "moved from")

    def testIntegCopyAndRenameAsAdd(self):
        """Test for integrating a copy and move into single target - with target action add."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        create_file(file1, "Test content")
        create_file(file2, "Test content2")
        self.source.p4cmd('add', file1, file2)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', file2)
        self.source.p4cmd('move', file2, file3)
        self.source.p4cmd('integrate', file1, file3)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'file3 added')

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "moved from")

        recs = self.dumpDBFiles("db.integed,db.rev,db.revhx")
        self.logger.debug(recs)
        # @pv@ 0 @db.integed@ @//stream/main/file1@ @//stream/main/file3@ 0 1 0 1 10 5
        # @pv@ 0 @db.integed@ @//stream/main/file2@ @//stream/main/file3@ 0 1 0 1 15 5
        # @pv@ 0 @db.integed@ @//stream/main/file3@ @//stream/main/file1@ 0 1 0 1 4 5
        # @pv@ 0 @db.integed@ @//stream/main/file3@ @//stream/main/file2@ 0 1 0 1 14 5

        # @pv@ 9 @db.rev@ @//depot/inside/file3@ 1 0 8 2 1618248262 1618248262 8BFA8E0684108F419933A5995264D150 12 0 0 @//depot/inside/file3@ @1.2@ 0
        # @pv@ 9 @db.revhx@ @//depot/inside/file3@ 1 0 8 2 1618248262 1618248262 8BFA8E0684108F419933A5995264D150 12 0 0 @//depot/inside/file3@ @1.2@ 0

        # Convert copy -> branch, and move/add -> add - can't be done through front end

        newrecs = []
        for rec in recs:
            if "@db.integed@ @//depot/inside/file3@ @//depot/inside/file1@" in rec:
                rec = rec.replace("@ 0 1 0 1 4", "@ 0 1 0 1 2")     # 4->2
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.integed@ @//depot/inside/file1@ @//depot/inside/file3@" in rec:
                rec = rec.replace("@ 0 1 0 1 10", "@ 0 1 0 1 5")     # 10->5
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            # move/add -> add
            if "@db.rev@ @//depot/inside/file3@ 1 0 8" in rec:
                rec = rec.replace("@//depot/inside/file3@ 1 0 8", "@//depot/inside/file3@ 1 0 0")     # 10->5
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.revhx@ @//depot/inside/file3@ 1 0 8" in rec:
                rec = rec.replace("@//depot/inside/file3@ 1 0 8", "@//depot/inside/file3@ 1 0 0")     # 10->5
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            # move/delete -> delete
            if "@db.rev@ @//depot/inside/file1@ 1 0 7" in rec:
                rec = rec.replace("@//depot/inside/file1@ 1 0 7", "@//depot/inside/file1@ 1 0 2")
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.revhx@ @//depot/inside/file1@ 1 0 7" in rec:
                rec = rec.replace("@//depot/inside/file1@ 1 0 7", "@//depot/inside/file1@ 1 0 2")
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
        self.logger.debug("Newrecs:", "\n".join(newrecs))
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "branch from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "moved from")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(2, len(filelog.revisions[0].integrations))
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "moved from")

    def testIntegCopyAndRenameAsAddFromOutside(self):
        """Test for integrating a copy and move into single target - when copy is from outside view."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        outside_file4 = os.path.join(outside, "file4")
        outside_file5 = os.path.join(outside, "file5")
        create_file(file1, "Test content")
        create_file(file2, "Test content")
        create_file(outside_file4, "Test content")
        create_file(outside_file5, "Test content")
        self.source.p4cmd('add', file1, file2, outside_file4, outside_file5)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', outside_file5)
        append_to_file(outside_file5, '\nmore')
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', file2)
        self.source.p4cmd('move', file2, file3)
        self.source.p4cmd('integrate', '-f', "%s#2,2" % outside_file5, file3)
        self.source.p4cmd('resolve', '-am')
        self.source.p4cmd('integrate', '-f', outside_file4, file3)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'file3 added')

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(3, len(filelog.revisions[0].integrations))
        # self.assertEqual(filelog.revisions[0].integrations[0].how, "moved from")
        # self.assertEqual(filelog.revisions[0].integrations[1].how, "copy from")
        # self.assertEqual(filelog.revisions[0].integrations[2].how, "merged from")

        recs = self.dumpDBFiles("db.integed")
        self.logger.debug(recs)

        # @pv@ 0 @db.integed@ @//depot/inside/file3@ @//depot/outside/file5@ 1 2 0 1 6 3
        # @pv@ 0 @db.integed@ @//depot/outside/file5@ @//depot/inside/file3@ 0 1 1 2 10 3

        # Convert copy from -> merged from
        newrecs = []
        for rec in recs:
            if "@db.integed@ @//depot/inside/file3@ @//depot/outside/file5@" in rec:
                rec = rec.replace("@ 1 2 0 1 6", "@ 1 2 0 1 0")     # 6->0
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.integed@ @//depot/outside/file5@ @//depot/inside/file3@" in rec:
                rec = rec.replace("@ 0 1 1 2 10", "@ 0 1 1 2 1")     # 10->1
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
        self.logger.debug("Newrecs:", "\n".join(newrecs))
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog('//depot/inside/file3')[0]
        self.assertEqual(filelog.revisions[0].integrations[0].how, "moved from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[2].how, "merge from")

        self.run_P4Transfer()
        self.assertCounters(3, 2)

        filelog = self.target.p4.run_filelog('//depot/import/file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(1, len(filelog.revisions[0].integrations))
        self.assertEqual(filelog.revisions[0].integrations[0].how, "moved from")

    def testIgnoredDelete(self):
        """Test for ignoring a delete and then doing it again"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        create_file(file1, "Test content")

        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('integ', file1, file2)
        self.source.p4cmd('submit', '-d', 'file2 added')

        self.source.p4cmd('delete', file2)
        self.source.p4cmd('submit', '-d', 'file2 deleted')

        self.source.p4cmd('integ', '-Rd', file2, file1)
        self.source.p4cmd('resolve', '-ay')
        self.source.p4cmd('submit', '-d', 'file2 delete ignored')

        self.source.p4cmd('integ', '-f', file2, file1)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'file2 delete integrated')

        self.run_P4Transfer()
        self.assertCounters(5, 5)

    def testIgnoredDeleteInteg(self):
        """Test for ignoring a delete which is being integrated 0360 - and the resulting rev re-numbering"""
        self.setupTransfer()

        # file3
        # $ p4 filelog //Ocean/Console-0.12.4/T_Shark.uasset#2

        # //Ocean/Console-0.12.4/T_Shark.uasset#2
        # ... #2 change 11891294 integrate on 2020/03/03 by casey.spencer@ROBO (binary+l) 'Merging //Ocean/Release-0.13 to'
        # ... ... copy from //Ocean/Release-0.12.4/T_Shark.uasset#3
        # ... #1 change 11890184 branch on 2020/03/03 by casey.spencer@ROBO (binary+l) 'Merging //Ocean/Release-0.13 to'
        # ... ... branch from //Ocean/Release-0.12.4/T_Shark.uasset#2

        # file2
        # $ p4 filelog //Ocean/Release-0.12.4/T_Shark.uasset#3

        # //Ocean/Release-0.12.4/T_Shark.uasset
        # ... #3 change 11891280 integrate on 2020/03/03 by Casey.Spencer@CSws (binary+l) 'Merging //Ocean/Release-0.13 to'
        # ... ... copy from //Ocean/Release-0.13/T_Shark.uasset#5
        # ... #2 change 11890179 branch on 2020/03/03 by Casey.Spencer@CSws (binary+l) 'Merging //Ocean/Release-0.13 to'
        # ... ... branch from //Ocean/Release-0.13/T_Shark.uasset#4
        # ... #1 change 11887389 delete on 2020/03/03 by Casey.Spencer@CSws (binary+l) '@ocn Cherry picking CL 11882562'
        # ... ... ignored //Ocean/Release-0.13/T_Shark.uasset#4

        # file1
        # $ p4 filelog //Ocean/Release-0.13/T_Shark.uasset#4

        # //Ocean/Release-0.13/T_Shark.uasset
        # ... #4 change 11885487 edit on 2020/03/03 by emily.solomon@ESws (binary+l) '#jira nojira  Updating Shark an'
        # ... ... branch into //Ocean/Release-0.12.4/T_Shark.uasset#2
        # ... ... ignored by //Ocean/Release-0.12.4/T_Shark.uasset#1
        # ... #3 change 11872056 edit on 2020/03/03 by Emily.Solomon@ESws (binary+l) '#jira nojira Updating Elim Obje'
        # ... #2 change 11849715 edit on 2020/03/02 by emily.solomon@ESws (binary+l) '#jira nojira  Updating Elim Obj'
        # ... #1 change 11711950 add on 2020/02/27 by Emily.Solomon@ESws (binary+l) '#jira nojira Updating Snowman t'

        inside = localDirectory(self.source.client_root, "inside")
        # outside = localDirectory(self.source.client_root, "outside")
        file1 = os.path.join(inside, "file1")
        file2 = os.path.join(inside, "file2")
        file3 = os.path.join(inside, "file3")
        # outside_file1 = os.path.join(outside, 'outside_file1')
        create_file(file1, "Test content")
        # create_file(outside_file1, "Some content")

        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', file1)
        append_to_file(file1, '\nmore')
        self.source.p4cmd('submit', '-d', 'edited')
        self.source.p4cmd('edit', file1)
        append_to_file(file1, '\nmore')
        self.source.p4cmd('submit', '-d', 'edited')
        self.source.p4cmd('edit', file1)
        append_to_file(file1, '\nmore')
        self.source.p4cmd('submit', '-d', 'edited')

        # Generate a first rev which is an ignore of a delete - this will not be transferred.
        self.source.p4cmd('integ', '-Rb', '%s#4,4' % file1, file2)
        self.source.p4cmd('resolve', '-ay')
        self.source.p4cmd('submit', '-d', 'ignore delete')

        self.source.p4cmd('integ', '-f', '%s#4,4' % file1, file2)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'branched')

        self.source.p4cmd('integ', file2, file3)
        self.source.p4cmd('submit', '-d', 'branched')

        self.source.p4cmd('edit', file1)
        append_to_file(file1, '\nmore again')
        self.source.p4cmd('submit', '-d', 'edited')

        self.source.p4cmd('integ', file1, file2)
        self.source.p4cmd('resolve', '-am')
        self.source.p4cmd('submit', '-d', 'branched')

        self.source.p4cmd('integ', file2, file3)
        self.source.p4cmd('resolve', '-am')
        self.source.p4cmd('submit', '-d', 'branched')

        filelog = self.source.p4.run_filelog('//depot/inside/file2')[0]
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[1].integrations[0].how, "branch from")
        self.assertEqual(filelog.revisions[2].integrations[0].how, "ignored")
        self.assertEqual(filelog.revisions[0].action, "integrate")
        self.assertEqual(filelog.revisions[1].action, "branch")
        self.assertEqual(filelog.revisions[2].action, "delete")

        self.run_P4Transfer()
        self.assertCounters(10, 10)

    def testIntegDirtyCopy(self):
        """Test for a copy which is then edited."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nmore stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('integrate', "%s#1" % inside_file1, inside_file2)
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('integrate', "%s#2,2" % inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-at', inside_file2)
        append_to_file(inside_file2, '\nextra stuff')
        self.source.p4cmd('submit', '-d', 'inside_file2 added with multiple integrates')

        filelog = self.source.p4.run_filelog(inside_file2)[0]
        self.logger.debug(filelog)

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "ignored")

    def testIntegAddCopy(self):
        """Test for and add and copy into same revision."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nmore stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('integrate', "%s#1" % inside_file1, inside_file2)
        self.source.p4cmd('integrate', "%s#2,2" % inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-at', inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added with multiple integrates')

        filelog = self.source.p4.run_filelog(inside_file2)[0]
        self.logger.debug(filelog)

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "ignored")

    def testIntegBranchAndCopy(self):
        """Branch a file and copy it as well into same revision."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'branch original')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 deleted')

        self.source.p4cmd('edit', inside_file2)
        append_to_file(inside_file2, "\nmore stuff")
        self.source.p4cmd('submit', '-d', 'inside_file2 edited')

        class EditAcceptTheirs(P4.Resolver):
            """
            Required because of a special 'force' flag which is set if this is done interactively - doesn't
            do the same as if you just resolve -at. Yuk!
            """
            def actionResolve(self, mergeInfo):
                return 'at'

        self.source.p4cmd('integrate', inside_file2, inside_file1)
        self.source.p4.run_resolve(resolver=EditAcceptTheirs())
        self.source.p4cmd('integrate', '-f', inside_file2, inside_file1)
        self.source.p4.run_resolve(resolver=EditAcceptTheirs())
        self.source.p4cmd('submit', '-d', 'inside_file1 added back with multiple integrates')

        filelog = self.source.p4.run_filelog(inside_file1)[0]
        self.logger.debug(filelog)
        self.assertEqual(2, len(filelog.revisions[0].integrations), 2)
        self.assertEqual("branch from", filelog.revisions[0].integrations[0].how)
        self.assertEqual("copy from", filelog.revisions[0].integrations[1].how)

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')[0]
        self.logger.debug(filelog)
        self.assertEqual(1, len(filelog.revisions[0].integrations))
        self.assertEqual("copy from", filelog.revisions[0].integrations[0].how)

    def testBranchAndMoveSameTarget(self):
        """Branch a file and move/rename into same revision."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'branch original')

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('move', inside_file1, inside_file3)
        self.source.p4cmd('integ', '-f', inside_file2, inside_file3)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', 'branch and move')

        filelog = self.source.p4.run_filelog(inside_file3)[0]
        self.logger.debug(filelog)
        self.assertEqual(2, len(filelog.revisions[0].integrations), 2)
        self.assertEqual("moved from", filelog.revisions[0].integrations[0].how)
        self.assertEqual("copy from", filelog.revisions[0].integrations[1].how)

        # Convert copy from to branch from
        recs = self.dumpDBFiles("db.integed")
        self.logger.debug(recs)

        #  '@pv@ 1 @db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file3@ 0 1 0 1 10 3 ', 
        #  '@pv@ 1 @db.integed@ @//depot/inside/inside_file3@ @//depot/inside/inside_file2@ 0 1 0 1 4 3 ']
        newrecs = []
        for rec in recs:
            if "@db.integed@ @//depot/inside/inside_file2@ @//depot/inside/inside_file3@" in rec:
                rec = rec.replace("@ 0 1 0 1 10", "@ 0 1 0 1 3")     # 10->3: edit into->branch into
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
            if "@db.integed@ @//depot/inside/inside_file3@ @//depot/inside/inside_file2@" in rec:
                rec = rec.replace("@ 0 1 0 1 4", "@ 0 1 0 1 2")     # 4->2: copy from->branch from
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
        self.logger.debug("Newrecs:", "\n".join(newrecs))
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog(inside_file3)[0]
        self.logger.debug(filelog)
        self.assertEqual(2, len(filelog.revisions[0].integrations), 2)
        self.assertEqual("moved from", filelog.revisions[0].integrations[0].how)
        self.assertEqual("branch from", filelog.revisions[0].integrations[1].how)

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(2, len(filelog.revisions[0].integrations))
        self.assertEqual("moved from", filelog.revisions[0].integrations[0].how)
        self.assertEqual("copy from", filelog.revisions[0].integrations[1].how)

    def testIntegAddMergeCopy(self):
        """Integrate an add/merge/copy of 3 revisions into single new target."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nmore stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nYet more stuff")
        self.source.p4cmd('submit', '-d', 'file edited again')

        self.source.p4cmd('integrate', "%s#1" % inside_file1, inside_file2)
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('integrate', "%s#3,3" % inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-am', inside_file2)
        self.source.p4cmd('integrate', "%s#2,2" % inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-am', inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added with multiple integrates')

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 3)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "merge from")
        self.assertEqual(filelog.revisions[0].integrations[2].how, "add from")

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 3)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "copy from")
        self.assertEqual(filelog.revisions[0].integrations[1].how, "ignored")
        self.assertEqual(filelog.revisions[0].integrations[2].how, "ignored")

    def testIntegSelectiveWithEdit(self):
        """Integrate cherry picked rev into a file."""
        self.setupTransfer()
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-tbinary', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        create_file(inside_file2, "Test content 2")
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nmore stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "\nYet more stuff")
        self.source.p4cmd('submit', '-d', 'file edited again')

        self.source.p4cmd('integrate', "%s#2,3" % inside_file1, inside_file2)

        class EditResolve(P4.Resolver):
            def resolve(self, mergeData):
                result_path = mergeData.result_path
                if not result_path:
                    result_path = mergeData.your_path
                create_file(result_path, """
        Line 1
        Line 2 - changed
        Line 3 - edited
        """)
                return 'ae'

        self.source.p4.run_resolve(resolver=EditResolve())
        self.source.p4cmd('submit', '-d', "Merge with edit")

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 1)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "edit from")

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 1)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "edit from")

    def testIntegDeleteProblem(self):
        "Reproduce a problem where integrate was resulting in a delete"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        inside_file4 = os.path.join(inside, "inside_file4")
        create_file(inside_file1, 'Test content')
        create_file(inside_file3, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('add', inside_file3)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('edit', inside_file3)
        append_to_file(inside_file1, "more content")
        append_to_file(inside_file3, "more content")
        self.source.p4cmd('submit', '-d', 'files edited')

        self.source.p4cmd('integrate', '-2', inside_file1, inside_file2)
        self.source.p4cmd('integrate', '-2', inside_file3, inside_file4)
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('add', inside_file4)
        append_to_file(inside_file4, "and some more")
        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('delete', inside_file3)
        self.source.p4cmd('submit', '-d', 'renamed old way')

        self.source.p4cmd('integrate', '-2', '-f', inside_file2, inside_file1)
        self.source.p4cmd('integrate', '-2', '-f', inside_file4, inside_file3)
        self.source.p4cmd('add', inside_file3)
        append_to_file(inside_file3, "yet more")
        self.source.p4cmd('delete', inside_file2)
        self.source.p4cmd('delete', inside_file4)
        self.source.p4cmd('submit', '-d', 'renamed back again')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'add')
        self.assertEqual(filelog[0].revisions[1].action, 'delete')
        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].action, 'delete')
        self.assertEqual(filelog[0].revisions[1].action, 'add')
        filelog = self.target.p4.run_filelog('//depot/import/inside_file3')
        self.assertEqual(filelog[0].revisions[0].action, 'add')
        self.assertEqual(filelog[0].revisions[1].action, 'delete')
        filelog = self.target.p4.run_filelog('//depot/import/inside_file4')
        self.assertEqual(filelog[0].revisions[0].action, 'delete')
        self.assertEqual(filelog[0].revisions[1].action, 'add')

    def testAddFrom(self):
        """Test for adding a file which has in itself then branched."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 deleted')

        self.source.p4cmd('sync', "%s#1" % inside_file1)
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('move', inside_file1, inside_file2)
        os.chmod(inside_file2, stat.S_IWRITE + stat.S_IREAD)
        append_to_file(inside_file2, "more stuff")
        self.source.p4cmd('submit', '-d', 'inside_file2 created by branching with add')

        filelog = self.source.p4.run_filelog(inside_file2)[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 1)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "add from")

        self.source.p4cmd('integrate', inside_file2, inside_file3)
        self.source.p4cmd('submit', '-d', 'inside_file3 created as copy')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 2)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "add from")

    def testDeleteDelete(self):
        """Test for a delete on top of a delete."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        create_file(inside_file1, "Test content")
        create_file(inside_file2, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('add', inside_file2)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('integrate', inside_file2, inside_file3)
        self.source.p4cmd('submit', '-d', 'branched file')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('delete', inside_file2)
        self.source.p4cmd('submit', '-d', 'files deleted')

        with self.source.p4.at_exception_level(P4.P4.RAISE_ERRORS):
            self.source.p4cmd('sync', "%s#1" % '//depot/inside/inside_file1')
            self.source.p4cmd('sync', "%s#1" % '//depot/inside/inside_file2')
            self.source.p4cmd('delete', '//depot/inside/inside_file1')
            self.source.p4cmd('delete', '//depot/inside/inside_file2')
        self.source.p4cmd('opened')
        try:
            self.source.p4cmd('submit', '-d', 'files deleted again')
        except Exception as e:
            self.logger.info(str(e))
            err = self.source.p4.errors[0]
            if re.search("Submit failed -- fix problems above then", err):
                m = re.search(r"p4 submit -c (\d+)", err)
                if m:
                    self.source.p4cmd('submit', '-c', m.group(1))

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file1')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        self.assertEqual(filelog.revisions[1].action, 'delete')

        self.source.p4cmd('integrate', inside_file2, inside_file3)
        self.source.p4cmd('submit', '-d', 'integrated delete')

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        self.assertEqual(filelog.revisions[1].action, 'delete')

    def testIntegDeleteIgnore(self):
        """Test for an integrated delete with ignore."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        inside_file4 = os.path.join(inside, "inside_file4")
        outside_file1 = os.path.join(outside, 'outside_file1')
        outside_file2 = os.path.join(outside, 'outside_file2')
        create_file(inside_file1, "Test content")
        create_file(inside_file4, "Test content")
        create_file(outside_file1, "Some content")
        create_file(outside_file2, "Some content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('add', inside_file4)
        self.source.p4cmd('add', outside_file1)
        self.source.p4cmd('add', outside_file2)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('delete', inside_file4)
        self.source.p4cmd('submit', '-d', 'deleted file')

        self.source.p4cmd('integ', '-Rb', inside_file1, inside_file2)
        self.source.p4cmd('integ', '-Rb', outside_file1, inside_file3)
        self.source.p4cmd('integ', '-Rb', outside_file2, inside_file4)
        self.source.p4cmd('resolve', '-ay')
        self.source.p4cmd('submit', '-d', 'inside_files deleted')

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        filelog = self.source.p4.run_filelog('//depot/inside/inside_file3')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        filelog = self.source.p4.run_filelog('//depot/inside/inside_file4')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        self.assertEqual(filelog.revisions[1].action, 'delete')

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        files = self.target.p4.run_files('//depot/import/...')
        self.logger.debug(files)
        self.assertEqual(len(files), 3)
        self.assertEqual(files[0]['depotFile'], '//depot/import/inside_file1')
        self.assertEqual(files[1]['depotFile'], '//depot/import/inside_file2')
        self.assertEqual(files[2]['depotFile'], '//depot/import/inside_file4')

    def testIntegDeleteOverDelete(self):
        """A delete is integrated on top of a delete - only possible in certain older servers."""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('integ', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'create file2')

        self.source.p4cmd('delete', inside_file1)
        self.source.p4cmd('submit', '-d', 'deleted file1')

        self.source.p4cmd('edit', inside_file2)
        self.source.p4cmd('submit', '-d', 'edit/delete file2')

        self.source.p4cmd('integ', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-at', inside_file2)
        self.source.p4cmd('submit', '-d', 'deleted file2')

        # We change rev2 from an edit to a delete by replacing the rev2 record with the adjusted rev3 - dodgy!
        recs = self.dumpDBFiles("db.rev")
        self.logger.debug(recs)
        # @pv@ 9 @db.rev@ @//depot/inside/inside_file2@ 3 0 2 5 1430226630 0 00000000000000000000000000000000 -1 0 0 @//depot/inside/inside_file2@ @1.5@ 0
        # @pv@ 9 @db.rev@ @//depot/inside/inside_file2@ 2 0 1 4 1430226630 1430226630 8BFA8E068410...5264D150 12 0 0 @//depot/inside/inside_file2@ @1.4@ 0
        newrecs = []
        for rec in recs:
            if "@db.rev@ @//depot/inside/inside_file2@ 3" in rec:
                rec = rec.replace("@ 3 0 2 5", "@ 2 0 2 4")     # chg 5->4,
                rec = rec.replace("@1.5@", "@1.4@")             # lbrRev
                rec = rec.replace("@pv@", "@rv@")
                newrecs.append(rec)
        self.logger.debug("Newrecs:", "\n".join(newrecs))
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')
        self.assertEqual(filelog.revisions[0].integrations[0].how, "delete from")
        self.assertEqual(filelog.revisions[1].action, 'delete')

        self.run_P4Transfer()
        self.assertCounters(5, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(filelog.revisions[0].action, 'delete')

    def testIntegIgnoreAndEdit(self):
        "Test an ignore where file is actually changed"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'inside_file2 added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, "more stuff")
        self.source.p4cmd('submit', '-d', 'inside_file1 edited')

        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('resolve', '-ay')
        self.source.p4cmd('edit', inside_file2)
        append_to_file(inside_file2, 'Different stuff')
        self.source.p4cmd('submit', '-d', 'integrated ignore and edited')

        filelog = self.source.p4.run_filelog(inside_file2)
        self.assertEqual(filelog[0].revisions[0].action, 'edit')

        recs = self.dumpDBFiles("db.rev,db.revcx,db.revhx")
        self.logger.debug(recs)
        # @pv@ 9 @db.rev@ @//depot/inside/inside_file2@ 2 0 1 4 1423760150 1423760150 1141C16D0...BA151F6054F1D8 28 0 0 @//depot/inside/inside_file2@ @1.4@ 0
        # @pv@ 0 @db.revcx@ 4 @//depot/inside/inside_file2@ 2 1
        # @pv@ 9 @db.revhx@ @//depot/inside/inside_file2@ 2 0 1 4 1423760150 1423760150 1141C16D0...BA151F6054F1D8 28 0 0 @//depot/inside/inside_file2@ @1.4@ 0
        # Change action record from 1 (edit) to 4 (integ)
        newrecs = []
        for rec in recs:
            if "@db.rev@ @//depot/inside/inside_file2@ 2 0 1 4" in rec:
                rec = rec.replace("@ 2 0 1 4 ", "@ 2 0 4 4 ")
                newrecs.append(rec.replace("@pv@", "@rv@"))
            elif "@db.revcx@ 4 @//depot/inside/inside_file2@ 2 1" in rec:
                rec = rec.replace("@ 2 1", "@ 2 4")
                newrecs.append(rec.replace("@pv@", "@rv@"))
            elif "@db.revhx@ @//depot/inside/inside_file2@ 2 0 1 4" in rec:
                rec = rec.replace("@ 2 0 1 4 ", "@ 2 0 4 4 ")
                newrecs.append(rec.replace("@pv@", "@rv@"))
        self.logger.debug(newrecs)
        self.applyJournalPatch("\n".join(newrecs))

        filelog = self.source.p4.run_filelog(inside_file2)
        self.logger.debug(filelog)
        self.assertEqual(filelog[0].revisions[0].action, 'integrate')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')[0]
        self.logger.debug(filelog)
        self.assertEqual(len(filelog.revisions[0].integrations), 1)
        self.assertEqual(filelog.revisions[0].integrations[0].how, "ignored")
        self.assertEqual(filelog.revisions[0].action, "edit")

    def testIntegI(self):
        """Test for integ -i required - only necessary with older integ.engine"""
        self.setupTransfer()
        self.target.p4cmd("configure", "set", "dm.integ.engine=2")

        inside = localDirectory(self.source.client_root, "inside")
        original_file = os.path.join(inside, 'original', 'original_file')
        new_file = os.path.join(inside, 'branch', 'new_file')
        create_file(original_file, "Some content")
        create_file(new_file, "Some new content")

        self.source.p4cmd('add', original_file)
        self.source.p4cmd('submit', '-d', "adding file1")

        self.source.p4cmd('add', new_file)
        self.source.p4cmd('submit', '-d', "adding file2")

        self.source.p4cmd('edit', original_file)
        create_file(original_file, "Some content addition")
        self.source.p4cmd('submit', '-d', "adding file1")

        self.source.p4cmd('integ', original_file, new_file)
        self.source.p4cmd('resolve', '-at')
        self.source.p4cmd('submit', '-d', "adding file2")

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        change = self.target.p4.run_describe('4')[0]
        self.logger.debug(change)
        self.assertEqual(len(change['depotFile']), 1)
        self.assertEqual(change['depotFile'][0], '//depot/import/branch/new_file')
        self.assertEqual(change['action'][0], 'integrate')

    def testObliteratedSource(self):
        "File has been integrated and then source obliterated"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        outside = localDirectory(self.source.client_root, "outside")
        outside_file1 = os.path.join(outside, 'outside_file1')
        create_file(outside_file1, "Some content")
        self.source.p4cmd('add', outside_file1)
        self.source.p4cmd('submit', '-d', "Outside outside_file1")

        inside_file2 = os.path.join(inside, 'inside_file2')
        self.source.p4cmd('integrate', outside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', "Integrated from outside to inside")

        self.source.p4cmd('edit', outside_file1)
        append_to_file(outside_file1, "More text")
        self.source.p4cmd('submit', '-d', "edit outside")

        self.source.p4cmd('integrate', outside_file1, inside_file2)
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('submit', '-d', "integrated inside")

        self.source.p4.run_obliterate('-y', outside_file1)

        filelog = self.source.p4.run_filelog('//depot/inside/inside_file2')
        self.assertEqual(filelog[0].revisions[0].action, 'integrate')
        self.assertEqual(filelog[0].revisions[1].action, 'branch')

        self.run_P4Transfer()
        self.assertCounters(4, 2)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file2')
        self.assertEqual(filelog[0].revisions[0].action, 'edit')
        self.assertEqual(filelog[0].revisions[1].action, 'add')

    def testKeywords(self):
        "Look for files with keyword expansion"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")

        files = []
        for f in range(1, 5):
            fname = "file{}".format(f)
            files.append(os.path.join(inside, fname))

        for fname in files:
            create_file(fname, 'Test content')
            self.source.p4cmd('add', fname)

        self.source.p4.run_reopen("-t", "ktext", files[0])
        self.source.p4.run_reopen("-t", "kxtext", files[1])
        self.source.p4.run_reopen("-t", "text+kmx", files[2])
        self.source.p4.run_reopen("-t", "ktext+xkm", files[3])

        self.source.p4cmd('submit', '-d', 'File(s) added')

        self.run_P4Transfer("--nokeywords")

        newFiles = self.target.p4cmd('files', "//...")
        self.assertEqual(len(newFiles), 4)
        self.assertEqual(newFiles[0]['type'], "text")
        self.assertTrue(newFiles[1]['type'] in ["xtext", "text+x"])
        self.assertEqual(newFiles[2]['type'], "text+mx")
        self.assertEqual(newFiles[3]['type'], "text+mx")

        fname = files[0]
        self.source.p4cmd('edit', "-t", "+k", fname)
        append_to_file(fname, "More stuff")

        self.source.p4cmd('submit', '-d', 'File edited')

        self.run_P4Transfer("--nokeywords")

        files = self.target.p4cmd('files', "//depot/import/file1")
        self.assertEqual(files[0]['type'], "text")

    def testResetConnection(self):
        "Reset connection - for flaky connections"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")

        files = []
        for f in range(1, 5):
            fname = "file{}".format(f)
            files.append(os.path.join(inside, fname))

        for fname in files:
            create_file(fname, 'Test content')
            self.source.p4cmd('add', fname)

        self.source.p4cmd('submit', '-d', 'File(s) added')

        self.run_P4Transfer("--reset-connection", "2")

        newFiles = self.target.p4cmd('files', "//...")
        self.assertEqual(len(newFiles), 4)

    def testTempobjFiletype(self):
        """Tests for files with no content"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        inside_file2 = os.path.join(inside, "inside_file2")
        inside_file3 = os.path.join(inside, "inside_file3")
        inside_file4 = os.path.join(inside, "inside_file4")

        create_file(inside_file1, "Test content")
        create_file(inside_file2, "Test content")
        create_file(inside_file3, "Test content")
        self.source.p4cmd('add', '-t', 'text+S2', inside_file1)
        self.source.p4cmd('add', '-t', 'binary+S', inside_file2)
        self.source.p4cmd('add', '-t', 'binary+S', inside_file3)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('integrate', inside_file3, inside_file4)
        self.source.p4cmd('submit', '-d', 'integrated')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, 'New text')
        self.source.p4cmd('delete', inside_file2)
        self.source.p4cmd('submit', '-d', 'version 2')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, 'More text')
        self.source.p4cmd('edit', inside_file3)
        append_to_file(inside_file1, 'More textasdf')
        self.source.p4cmd('submit', '-d', 'version 3')

        self.source.p4cmd('integrate', inside_file3, inside_file4)
        self.source.p4cmd('resolve', '-as')
        self.source.p4cmd('submit', '-d', 'integrated')

        self.run_P4Transfer()
        self.assertCounters(5, 5)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        revisions = filelog[0].revisions
        self.logger.debug('test:', revisions)
        self.assertEqual(len(revisions), 3)
        for rev in revisions:
            self.logger.debug('test:', rev.rev, rev.action, rev.digest)
            self.logger.debug(self.target.p4.run_print('//depot/import/inside_file1#%s' % rev.rev))
        filelog = self.target.p4.run_filelog('//depot/import/inside_file4')
        self.assertEqual(filelog[0].revisions[0].action, 'integrate')
        self.assertEqual(filelog[0].revisions[1].action, 'purge')

    def testAddAfterPurge(self):
        """Tests for files added after being purged"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-t', 'text', inside_file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('sync', '@0')
        self.source.p4cmd('obliterate', '-yp', inside_file1)

        self.source.p4cmd('add', inside_file1)
        append_to_file(inside_file1, 'New text')
        self.source.p4cmd('submit', '-d', 'version 2')

        self.run_P4Transfer()
        self.assertCounters(2, 2)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        revisions = filelog[0].revisions
        self.logger.debug('test:', revisions)
        self.assertEqual(len(revisions), 2)
        for rev in revisions:
            self.logger.debug('test:', rev.rev, rev.action, rev.digest)
            self.logger.debug(self.target.p4.run_print('//depot/import/inside_file1#%s' % rev.rev))
        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'edit')
        self.assertEqual(filelog[0].revisions[1].action, 'add')

    def testBranchUndoAfterPurge(self):
        """Tests for files branched ontop of purged revs"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', '-t', 'text', inside_file1)
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('edit', inside_file1)
        append_to_file(inside_file1, 'New text')
        self.source.p4cmd('submit', '-d', 'version 2')

        self.source.p4cmd('sync', '@0')
        self.source.p4cmd('obliterate', '-yp', f'{inside_file1}#2')

        self.source.p4cmd('copy', f'{inside_file1}#1', inside_file1)
        self.source.p4cmd('submit', '-d', 'branch/undo')

        self.run_P4Transfer()
        self.assertCounters(3, 3)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        revisions = filelog[0].revisions
        self.logger.debug('test:', revisions)
        self.assertEqual(len(revisions), 3)
        for rev in revisions:
            self.logger.debug('test:', rev.rev, rev.action, rev.digest)
            self.logger.debug(self.target.p4.run_print('//depot/import/inside_file1#%s' % rev.rev))
        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        self.assertEqual(filelog[0].revisions[0].action, 'integrate')
        self.assertEqual(filelog[0].revisions[1].action, 'edit')

    def testBranchPerformance(self):
        "Branch lots of files and test performance"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        for i in range(99):
            file = os.path.join(inside, "original", "file%d" % i)
            create_file(file, "Test content")
        self.source.p4cmd('add', "//depot/inside/...")
        self.source.p4cmd('submit', '-d', 'files added')

        self.source.p4cmd('populate', "//depot/inside/original/...", "//depot/inside/new/...")

        self.run_P4Transfer()
        self.assertCounters(2, 2)

    def testBackoutMove(self):
        """In this test we move a file and then rollback to the previous changelist
        way that P4V does - this does an 'add -d'"""
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")

        create_file(inside_file1, "Test content")
        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('edit', inside_file1)
        self.source.p4cmd('move', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'moved inside_file1 to inside_file2')

        self.source.p4.run_sync('-f', '%s#1' % inside_file1)
        self.source.p4cmd('add', '-d', inside_file1)
        self.source.p4cmd('submit', '-d', 'new inside_file1')

        self.source.p4cmd('edit', inside_file1)
        create_file(inside_file1, "Different test content")
        self.source.p4cmd('submit', '-d', 'changed inside_file1 again')

        self.run_P4Transfer()
        self.assertCounters(4, 4)

        filelog = self.target.p4.run_filelog('//depot/import/inside_file1')
        revisions = filelog[0].revisions
        self.logger.debug('test:', revisions)
        self.assertEqual(len(revisions), 4)
        for rev in revisions:
            self.logger.debug('test:', rev.rev, rev.action, rev.digest)
            self.logger.debug(self.target.p4.run_print('//depot/import/inside_file1#%s' % rev.rev))
        for rev in self.source.p4.run_filelog('//depot/inside/inside_file1')[0].revisions:
            self.logger.debug('test-src:', rev.rev, rev.action, rev.digest)
            self.logger.debug(self.source.p4.run_print('//depot/inside/inside_file1#%s' % rev.rev))

        self.assertEqual(revisions[3].action, "add")
        self.assertEqual(revisions[1].action, "add")
        self.assertEqual(revisions[0].action, "edit")
        # 2 latest revisions should not be the same, but the revisions before and after back out
        # should be.
        self.assertEqual(revisions[1].digest, revisions[3].digest)
        self.assertNotEqual(revisions[0].digest, revisions[1].digest)

    def testAddProblem(self):
        "Trying to reproduce problem reported where a branch required a resolve"
        self.setupTransfer()

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', "-t", "xtext", inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.source.p4cmd('edit', inside_file1)
        with open(inside_file1, 'a') as f:
            print("more content", file=f)
        self.source.p4cmd('submit', '-d', 'inside_file1 updated')

        self.source.p4cmd('edit', inside_file1)
        with open(inside_file1, 'a') as f:
            print("even more content", file=f)
        self.source.p4cmd('submit', '-d', 'inside_file1 updated (again)')

        inside_file2 = os.path.join(inside, "inside_file2")
        self.source.p4cmd('integrate', inside_file1, inside_file2)
        self.source.p4cmd('submit', '-d', 'branched into inside_file2')

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes', )
        self.assertEqual(len(changes), 4, "Target does not have exactly four changes")

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 2, "Target does not have exactly two files")

        self.assertCounters(4, 4)

    def testAddUnicodeEnabled(self):
        "Testing basic add with unicode enabled on source and dest"

        self.source.enableUnicode()
        self.target.enableUnicode()

        options = self.getDefaultOptions()
        srcOptions = {"p4charset": "utf8"}
        targOptions = {"p4charset": "utf8"}
        self.createConfigFile(options=options, srcOptions=srcOptions, targOptions=targOptions)

        inside = localDirectory(self.source.client_root, "inside")
        inside_file1 = os.path.join(inside, "inside_file1")
        create_file(inside_file1, 'Test content')

        self.source.p4cmd('add', inside_file1)
        self.source.p4cmd('submit', '-d', 'inside_file1 added')

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes')
        self.assertEqual(len(changes), 1, "Target does not have exactly one change")
        self.assertEqual(changes[0]['change'], "1")

        files = self.target.p4cmd('files', '//depot/...')
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['depotFile'], '//depot/import/inside_file1')

        self.assertCounters(1, 1)

    def testStreamsBasic(self):
        "Test basic source/target being a stream"
        self.setupTransfer()

        d = self.source.p4.fetch_depot('src_streams')
        d['Type'] = 'stream'
        self.source.p4.save_depot(d)
        s = self.source.p4.fetch_stream('-t', 'mainline', '//src_streams/main')
        self.source.p4.save_stream(s)

        d = self.target.p4.fetch_depot('targ_streams')
        d['Type'] = 'stream'
        self.target.p4.save_depot(d)

        config = self.getDefaultOptions()
        config['views'] = []
        config['transfer_target_stream'] = '//targ_streams/transfer_target_stream'
        config['stream_views'] = [{'src': '//src_streams/main',
                                   'targ': '//targ_streams/main',
                                   'type': 'mainline',
                                   'parent': ''}]
        self.createConfigFile(options=config)

        c = self.source.p4.fetch_client(self.source.client_name)
        c['Stream'] = '//src_streams/main'
        self.source.p4.save_client(c)

        inside = localDirectory(self.source.client_root, "inside")

        file1 = os.path.join(inside, 'file1')
        create_file(file1, "Test content")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', "Added files")

        self.run_P4Transfer()
        self.assertCounters(2, 3)   # Normally (1,1) - Extras change due to stream creation

        changes = self.target.p4cmd('changes', '//targ_streams/...')
        self.assertEqual(len(changes), 1, "Not exactly one change on target")
        filelog = self.target.p4.run_filelog('//targ_streams/main/...')
        self.assertEqual(len(filelog), 1, "Not exactly one file on target")
        self.assertEqual(filelog[0].revisions[0].action, "add")

        expView = ['//src_streams/main/... //%s/src_streams/main/...' % TRANSFER_CLIENT]
        client = self.source.p4.fetch_client(TRANSFER_CLIENT)
        self.assertEqual(expView, client._view)

        client = self.target.p4.fetch_client(TRANSFER_CLIENT)
        self.assertEqual('//targ_streams/transfer_target_stream', client._stream)

        # Run again and expect no extra stream changes to be created
        self.run_P4Transfer()
        self.assertCounters(2, 3)

    def testStreamsMultiple(self):
        "Test source/target being streams with multiples"
        self.setupTransfer()

        d = self.source.p4.fetch_depot('src_streams')
        d['Type'] = 'stream'
        self.source.p4.save_depot(d)
        s = self.source.p4.fetch_stream('-t', 'mainline', '//src_streams/main')
        self.source.p4.save_stream(s)

        d = self.target.p4.fetch_depot('targ_streams')
        d['Type'] = 'stream'
        self.target.p4.save_depot(d)

        s = self.target.p4.fetch_stream('-t', 'mainline', '//targ_streams/main')
        self.target.p4.save_stream(s)

        config = self.getDefaultOptions()
        config['views'] = []
        config['transfer_target_stream'] = '//targ_streams/transfer_target_stream'
        config['stream_views'] = [{'src': '//src_streams/rel*',
                                   'targ': '//targ_streams/rel*',
                                   'type': 'release',
                                   'parent': '//targ_streams/main'}]
        self.createConfigFile(options=config)

        c = self.source.p4.fetch_client(self.source.client_name)
        c['Stream'] = '//src_streams/main'
        self.source.p4.save_client(c)

        inside = localDirectory(self.source.client_root, "inside")

        file1 = os.path.join(inside, 'file1')
        create_file(file1, "Test content")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', "Added files")

        self.run_P4Transfer()
        self.assertCounters(0, 1)

        s = self.source.p4.fetch_stream('-t', 'release', '-P', '//src_streams/main', '//src_streams/rel1')
        self.source.p4.save_stream(s)

        s = self.source.p4.fetch_stream('-t', 'release', '-P', '//src_streams/main', '//src_streams/rel2')
        self.source.p4.save_stream(s)

        self.source.p4.run_populate('-S', '//src_streams/rel1', '-r')
        self.source.p4.run_populate('-S', '//src_streams/rel2', '-r')

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes', '//targ_streams/...')
        self.assertEqual(2, len(changes))
        filelog = self.target.p4.run_filelog('//targ_streams/rel1/...')
        self.assertEqual(1, len(filelog))
        self.assertEqual(filelog[0].revisions[0].action, "add")
        filelog = self.target.p4.run_filelog('//targ_streams/rel2/...')
        self.assertEqual(1, len(filelog))
        self.assertEqual(filelog[0].revisions[0].action, "add")

        # Double wildcards
        config['stream_views'] = [{'src': '//src_*/rel*',
                                   'targ': '//targ_*/rel*',
                                   'type': 'release',
                                   'parent': '//targ_streams/main'}]
        self.createConfigFile(options=config)

        c = self.source.p4.fetch_client(self.source.client_name)
        c['Stream'] = '//src_streams/rel1'
        self.source.p4.save_client(c)

        file1 = os.path.join(inside, 'file2')
        create_file(file1, "Test content")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', "Added file2")

        self.run_P4Transfer()

        changes = self.target.p4cmd('changes', '//targ_streams/...')
        self.assertEqual(3, len(changes))
        filelog = self.target.p4.run_filelog('//targ_streams/rel1/...')
        self.assertEqual(2, len(filelog))
        self.assertEqual("add", filelog[0].revisions[0].action)
        self.assertEqual("add", filelog[1].revisions[0].action)
        filelog = self.target.p4.run_filelog('//targ_streams/rel2/...')
        self.assertEqual(1, len(filelog))
        self.assertEqual("add", filelog[0].revisions[0].action)

        # Now change source streams, and replicate again - expecting new stream to be picked up
        s = self.source.p4.fetch_stream('-t', 'release', '-P', '//src_streams/main', '//src_streams/rel3')
        self.source.p4.save_stream(s)
        self.source.p4.run_populate('-S', '//src_streams/rel3', '-r')

        self.run_P4Transfer()
        self.assertCounters(9, 10)

        changes = self.target.p4cmd('changes', '//targ_streams/...')
        self.assertEqual(4, len(changes))
        filelog = self.target.p4.run_filelog('//targ_streams/rel3/...')
        self.assertEqual(1, len(filelog))
        self.assertEqual("add", filelog[0].revisions[0].action)

    def testStreamsMultipleIndividual(self):
        "Test source/target being streams with multiple source/target matching"
        self.setupTransfer()

        d = self.source.p4.fetch_depot('src_streams')
        d['Type'] = 'stream'
        self.source.p4.save_depot(d)
        s = self.source.p4.fetch_stream('-t', 'mainline', '//src_streams/main')
        self.source.p4.save_stream(s)

        d = self.target.p4.fetch_depot('targ_streams')
        d['Type'] = 'stream'
        self.target.p4.save_depot(d)

        s = self.target.p4.fetch_stream('-t', 'mainline', '//targ_streams/main')
        self.target.p4.save_stream(s)

        config = self.getDefaultOptions()
        config['views'] = []
        config['transfer_target_stream'] = '//targ_streams/transfer_target_stream'
        config['stream_views'] = [{'src': '//src_streams/main',
                                   'targ': '//targ_streams/main',
                                   'type': 'mainline',
                                   'parent': ''},
                                  {'src': '//src_streams/rel1',
                                   'targ': '//targ_streams/rel1',
                                   'type': 'release',
                                   'parent': '//targ_streams/main'},
                                  {'src': '//src_streams/rel2',
                                   'targ': '//targ_streams/rel2',
                                   'type': 'release',
                                   'parent': '//targ_streams/main'}]
        self.createConfigFile(options=config)

        c = self.source.p4.fetch_client(self.source.client_name)
        c['Stream'] = '//src_streams/main'
        self.source.p4.save_client(c)

        inside = localDirectory(self.source.client_root, "inside")

        file1 = os.path.join(inside, 'file1')
        create_file(file1, "Test content")
        self.source.p4cmd('add', file1)
        self.source.p4cmd('submit', '-d', "Added files")

        s = self.source.p4.fetch_stream('-t', 'release', '-P', '//src_streams/main', '//src_streams/rel1')
        self.source.p4.save_stream(s)
        s = self.source.p4.fetch_stream('-t', 'release', '-P', '//src_streams/main', '//src_streams/rel2')
        self.source.p4.save_stream(s)
        self.source.p4.run_populate('-S', '//src_streams/rel1', '-r')
        self.source.p4.run_populate('-S', '//src_streams/rel2', '-r')

        self.run_P4Transfer()
        self.assertCounters(6, 7)

        changes = self.target.p4cmd('changes', '//targ_streams/...')
        self.assertEqual(3, len(changes))
        filelog = self.target.p4.run_filelog('//targ_streams/rel1/...')
        self.assertEqual(1, len(filelog))
        self.assertEqual("branch", filelog[0].revisions[0].action)
        filelog = self.target.p4.run_filelog('//targ_streams/rel2/...')
        self.assertEqual(1, len(filelog))
        self.assertEqual("branch", filelog[0].revisions[0].action)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--p4d', default=P4D)
    parser.add_argument('unittest_args', nargs='*')

    args = parser.parse_args()
    if args.p4d != P4D:
        P4D = args.p4d

    # Now set the sys.argv to the unittest_args (leaving sys.argv[0] alone)
    unit_argv = [sys.argv[0]] + args.unittest_args
    unittest.main(argv=unit_argv)
