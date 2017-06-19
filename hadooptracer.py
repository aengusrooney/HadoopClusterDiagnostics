#!/usr/bin/env python

# Copyright 2015 SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# 
# 
# 1.Make sure the Linux package 'strace' is installed on your Hive Node
# 
# Run the following command to install if the package 'strace' is not installed
# 
#     sudo yum install strace
# 
# 2.Transfer the following file to the Hive Node
# 
#     hadooptracer.py
# 
# 
# 3.Set the files to execute mode
# 
#     chmod 755 hadooptracer.py
# 
# 
# 4.Change directory to your user home directory
# 
#     cd ~
# 
# 5.Run the batch script with a userid that has Admin rights on the Hive Server:
# 
#     python ./hadooptracer.py -f /tmp/test.json
# 
# 
# 6.Transfer the files from the Hive Node to the workstation where you are running the SAS
# 
#   tmp/sitexmls to directory referenced by SAS_HADOOP_CONFIG_PATH
# 
#   tmp/jars/ to directory referenced by SAS_HADOOP_JAR_PATH
# 
# 
# 
# If the following Manual instructions fail, please zip the following files and send as an email attachment:
# 
#   SDM*.log
#   sashadoopconfig*.log
#   sasdm*.log .
# 
# Depending on the OS, these files will be located:
# 
# Windows:  "C:\Users\<userid>\AppData\Local\SAS\SASDeploymentWizard"
# Linux:    "/home/sasinst/.SASAppData/SASDeploymentWizard"
# 
# 

############################################################
#
#   hadooptracer.py
#
#   A script to help enumerate client side jars for common
#   hadoop services.
#   * hadoop
#   * hbase
#   * hcatalog
#   * hcatapi
#   * webhcat
#   * hive
#   * beeline
#   * mapreduce
#   * pig
#   * oozie
#   * thrift
#
#   See SASpedia for further documentation:
#       http://sww.sas.com/saspedia/HadoopTracer
#
############################################################

import ast
import copy
import json
import getpass
import glob
import logging
import os
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import time
import traceback
from optparse import OptionParser
from pprint import pprint
from string import Template
import subprocess
from subprocess import PIPE
from subprocess import Popen
from multiprocessing import Process, Queue
from distutils.version import LooseVersion

############################################################
#   GLOBALS
############################################################

# store intermediate work files here
WORKDIR = "$WORKDIR"

# wait this long for a command to finish
TIMEOUT = "180s"

# strace these commands
SERVICES = {'hadoop': 'hadoop fs -ls /',
            'hadoop-put': {'pre': 'dd if=/dev/urandom of=%s/dd.out bs=1K count=1' % (WORKDIR),
                           'cmd': 'hadoop fs -put -f %s/dd.out /tmp/%s-dd.out' % (WORKDIR, getpass.getuser()),
                           'post': 'hadoop fs -rm -f -skipTrash /tmp/%s-dd.out' % (getpass.getuser())},
            'pig': {'class': 'PigTrace'},
            'thrift': {'class': 'ThriftTrace'},
            'hbase': {'pre': 'echo "whoami\nexit" > %s/test.hBase' % WORKDIR,
                      'cmd': 'hbase shell %s/test.hBase' % WORKDIR},
            'yarn-node': 'yarn node -list',
            'yarn-apps': 'yarn application -list',
            'mapreduce': {'class': 'MapReduceTrace'},
            'hcatalog': {'class': 'HcatalogTrace'},
            'hcatapi': {'class': 'HcatAPITrace'},
            'hivejdbc': {'class': 'HiveJdbcTrace'},
            'beeline': {'class': 'BeelineJdbcTrace'},
            'oozie': {'class': 'OozieTrace'}
           }

# temporary cache for reruns
DATACACHE = {}


# global cache of jar contents
JCCACHE = {}
JCEXCLUSIONS = []

# create logging object
LOG = logging.getLogger()
LOG.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s hadooptracer [%(levelname)s] %(message)s')
ch.setFormatter(formatter)
LOG.addHandler(ch)


############################################################
#   TRACER CLASS
############################################################

class Tracer(object):
    """ A stub class for other tracer classes """

    def __init__(self):
        # internal data
        self.options = None
        self.workdir = WORKDIR
        self.cmddict = Tracer.get_cmd_paths()

        # strace attributes
        self.svckey = None
        self.tracecmd = None
        self.precmd = None
        self.postcmd = None

        # return data
        self.rc_strace = None
        self.rc_verbose = None
        self.verbose_errorlines = []
        self.rawdata = None
        self.jre = None
        self.classpath = None  #!verbose
        self.classpaths = None #verbose
        self.fqns = None
        self.javacmd = None
        self.javaenv = None
        self.jars = None
        self.jarfiles = None
        self.sitexmls = None
        self.version = None
        self.metadata = {}

    def SetWorkdir(self, workdir):
        self.workdir = workdir
        if not os.path.isdir(self.workdir):
            os.makedirs(self.workdir)

    def Run(self):

        """ Primary execution method. Is overridden by other tracers """

        self.FixCommands()

        # Handle pre-commands
        if self.precmd:
            LOG.info("%s - running pre-command" % self.svckey)
            run_command(self.precmd)

        LOG.info("%s - strace %s" % (self.svckey, self.tracecmd))
        self.strace(self.tracecmd, svckey=self.svckey, usetimeout=True)

        # Handle post-commands
        if self.postcmd:
            LOG.info("%s - running post-command" % self.svckey)
            run_command(self.postcmd)


    def FixCommands(self):
        """ Substitute the commands for their absolute paths """

        for x in ['precmd', 'tracecmd', 'postcmd']:
            if hasattr(self, x):
                y = getattr(self, x)
                if not y:
                    continue
                newargs = y
                args = y.split()
                for k,v in self.cmddict.iteritems():
                    if k in args:
                        newargs = newargs.replace("%s " % k, "%s " % v)

                if newargs != y:
                    setattr(self, x, newargs)


    @staticmethod
    def get_cmd_paths():

        """ A common problem on hadoop clusters is that various client
            scripts are not added to any default paths. Users have to
            run a find command or ask a DBA where the commands live.
            This function tries to find those missing commands """

        # FIXME - cache this dict to optimize/reduce syscalls
        cmddict = {'bash': getcmdpath('bash'),
                   'beeline': getcmdpath('beeline'),
                   'hadoop': getcmdpath('hadoop'),
                   'hbase': getcmdpath('hbase'),
                   'hcat': getcmdpath('hcat'),
                   'hdfs': getcmdpath('hdfs'),
                   'hive': getcmdpath('hive'),
                   'oozie': getcmdpath('oozie'),
                   'pig': getcmdpath('pig'),
                   'yarn': getcmdpath('yarn'),
                   'sqoop': getcmdpath('sqoop') }

        # Assemble a list of candidate paths relative to the hadoop
        # and hive bin directories because most projects and distros
        # are normally layed out in common directory structures.

        for k,v in cmddict.iteritems():
            if v:
                cmddict[k] = os.path.realpath(v)
            else:
                paths = []
                if cmddict['hive']:
                    hive = cmddict['hive']
                    paths.append(os.path.join(os.path.dirname(hive), k)),
                    paths.append(os.path.join(os.path.dirname(hive), '..', '..',
                                                             k, 'bin', k))
                    paths.append(os.path.join(os.path.dirname(hive), '..',
                                                             'hcatalog', 'bin', k))
                    paths.append(os.path.join(os.path.dirname(hive), '..', '..',
                                                             'hcatalog', 'bin', k))

                if cmddict['hadoop']:
                    hadoop = cmddict['hadoop']
                    paths.append(os.path.join(os.path.dirname(hadoop), '..', '..',
                                                             k, 'bin', k))

                # Validate each candidate and only keep ones that exist
                paths = [os.path.realpath(x) for x in paths]
                paths = [x for x in paths if os.path.isfile(x)]
                paths = sorted(set(paths))

                if len(paths) >= 1:
                    # Use the first match
                    cmddict[k] = paths[0]
                else:
                    # Fallback to the basename and leave it to the user to sort out
                    LOG.error("%s can not be found in the $PATH" % k)
                    cmddict[k] = k

                # Sometimes the scripts are not executable
                if not os.access(cmddict[k], os.X_OK):
                    cmddict[k] = "%s %s" % (cmddict['bash'], cmddict[k])

        return cmddict


    def strace(self, cmd, svckey=None, usetimeout=False, piping=True, shorten=False,
               use_hcp=False, logerrors=True, timeout=TIMEOUT):

        """ Strace a java command, rerun it with -verbose:class and save data """

        LOG.info("%s - calling strace" % svckey)
        rc, so, se = Tracer._strace(cmd, usetimeout=usetimeout, timeout=timeout,
                                    options=self.options, workdir=self.workdir)
        LOG.info("%s - strace rc: %s" % (svckey, rc))
        rawdata = str(so) + str(se)

        if self.options.noclean:
            fname = os.path.join(self.workdir, '%s.strace.out' % svckey)
            f = open(fname, 'wb')
            f.write(rawdata)
            f.close()

        LOG.info("%s - parsing java info" % svckey)
        JRE, CLASSPATH, JAVACMD, JAVAENV = parse_strace_output(rawdata)

        if not JRE or not CLASSPATH or not JAVACMD or not JAVAENV:
            LOG.error("%s - no jre/classpath/javacmd/javaenv" % svckey)
            return False

        # requote jdbc connection parameters
        for idx, x in enumerate(JAVACMD):
            if 'jdbc:hive' in x:
                if not x.startswith('"'):
                    x = '"' + x
                if not x.endswith('"'):
                    x = x + '"'
                if x != JAVACMD[idx]:
                    JAVACMD[idx] = x

        # cleanup the empty values
        CLASSPATH = ':'.join([x for x in CLASSPATH.split(':') if x])

        # Find and combine the HADOOP_CLASSPATH if allowed
        if use_hcp:
            HADOOP_CLASSPATH = get_hadoop_classpath(rawdata)
            if HADOOP_CLASSPATH:
                CLASSPATH = CLASSPATH + ':' + HADOOP_CLASSPATH

        if shorten:
            cpr = javaClasspathReducer(CLASSPATH)
            if cpr.shortenedclasspath:
                CLASSPATH = cpr.shortenedclasspath
                CLASSPATH = ':'.join(CLASSPATH)

        # Remove excluded packages (DL+derby workaround)
        if self.options.excludepackage and not self.options.noexclusions:
            LOG.info("%s - running jar exclusions: %s" % (svckey, self.options.excludepackage))
            CLASSPATH = exclude_packages(CLASSPATH, self.options.excludepackage)

        # Workaround for mapr 5.x sandboxes ...
        if svckey == "beeline":
            # mapr 5.x sandboxes use the hive standalone jar with beeline,
            # which gets excluded because of derby. This block attempts to
            # detect that and hopefully broaden the classpath with hive's
            # entire libdir to include the smaller jdbc client jars ...
            # if they exist.

            cp = CLASSPATH.split(':')
            for idx,x in enumerate(cp):
                if '*' in x:
                    tjars = glob.glob(x)
                    tjars = [y for y in tjars if y.endswith('.jar')]
                    for y in tjars:
                        if y not in cp:
                            cp.append(y)

            # Check if any jdbc jars in list and add globs for the
            # hive lib dir if not ...
            bns = [x for x in cp if '-jdbc' in os.path.basename(x)]
            if len(bns) == 0:
                dns = [os.path.dirname(x) for x in cp if 'hive' in os.path.basename(x)]
                dns = sorted(set(dns))
                for dn in dns:
                    CLASSPATH += ':' + dn + '/*'
                if not self.options.noexclusions:
                    CLASSPATH = exclude_packages(CLASSPATH, self.options.excludepackage)

        LOG.info("%s - parsing sitexmls" % svckey)
        sitexmls = parse_strace_open_file(rawdata, "site.xml", list=True)
        if not sitexmls:
            sitexmls = []

        # Get any conf dir references from the classpath
        classpath_dirs = [x for x in CLASSPATH.split(':') if not x.endswith('.jar') and not x.endswith('/*')]
        for cpd in classpath_dirs:
            xmlfiles = glob.glob('%s/*-site.xml' % cpd)
            xmlfiles = [os.path.realpath(x) for x in xmlfiles]
            if xmlfiles:
                sitexmls = sitexmls + xmlfiles

        # get the mapr.login.conf if defined
        maprlogin = parse_strace_open_file(rawdata, "login.conf")
        if maprlogin:
            LOG.info("%s - login.conf  %s" % (svckey, maprlogin))
            sitexmls.append(maprlogin)

        # get the mapr-clusters.conf if defined
        maprclusters = parse_strace_open_file(rawdata, "mapr-clusters.conf")
        if maprclusters:
            LOG.info("%s - mapr-clusters.conf %s" % (svckey, maprclusters))
            sitexmls.append(maprclusters)

        # Sort and unique the sitexmls
        sitexmls = sorted(set(sitexmls))

        LOG.info("%s - rerunning with -verbose:class" % svckey)
        vrc, rawdataj = javaverbose(self.options, CLASSPATH, JAVACMD,
                                    JAVAENV, piping=piping, svckey=svckey,
                                    usetimeout=usetimeout, timeout=timeout,
                                    workdir=self.workdir)
        LOG.info("%s - verbose rc: %s" % (svckey, vrc))
        LOG.info("%s - parsing -verbose:class output" % svckey)
        ECLASSPATH = parseverboseoutput(rawdataj)
        EJARS = classpathstojars(ECLASSPATH)
        EJARS = Tracer.jrejarfilter(JRE, EJARS)

        if self.options.noclean:
            fname = os.path.join(self.workdir, '%s.javaverbose.out' % svckey)
            f = open(fname, 'wb')
            f.write(rawdataj)
            f.close()

        # Show and or keep errors ...
        if vrc != 0:
            for x in rawdataj.split('\n'):
                if 'ERROR' in x:
                    self.verbose_errorlines.append(x)
                    if logerrors:
                        LOG.error("%s - %s" % (svckey, x))

        self.javacmd = JAVACMD
        self.javaenv = JAVACMD
        self.classpaths = ECLASSPATH
        self.fqns = ECLASSPATH
        self.jars = EJARS
        self.jarfiles = EJARS
        self.sitexmls = sitexmls
        self.rc_strace = rc
        self.rc_verbose = vrc

        if svckey:
            LOG.info("%s - strace finished (stracerc: %s verboserc: %s) " % (svckey, rc, vrc))


    @staticmethod
    def _strace(cmd, cwd=None, follow_threads=True, timeout=TIMEOUT, usetimeout=True, options=None, workdir=WORKDIR):

        """ Wrap input command with strace and return output """

        # Forcefully kill the command if it runs too long
        if usetimeout:
            timeoutcmd = None
            if checkcmdinpath('timeout') and not 'beeline' in cmd:
                timeoutcmd = getcmdpath('timeout')
                timeoutcmd = "%s -s SIGKILL %s" % (timeoutcmd, timeout)
            else:
                timeoutcmd = bashtimeout(workdir=workdir, timeout=timeout)

        if follow_threads:
            args = "strace -s 100000 -fftv -e trace=execve,open %s 2>&1" % (cmd)
        else:
            args = "strace -s 100000 -tv -e trace=execve,open %s 2>&1" % (cmd)

        if usetimeout:
            args = "%s %s" % (timeoutcmd, args)

        p = None
        if not options.verbose:
            #p = Popen(args, cwd=cwd, stdout=PIPE, stderr=PIPE, shell=True)
            p = Popen(args, cwd=cwd, stdout=PIPE, stderr=subprocess.STDOUT, shell=True)
            so, se = p.communicate()
            rc = p.returncode
        else:
            (rc, so, se) = run_command_live(args)

        return rc, so, se


    @staticmethod
    def get_jdk_jre_jar_commands():

        jdk = None
        jre = None
        jarcmd = None

        # setup necessary java tools
        jdkbasedir = locatejdkbasedir()
        if jdkbasedir:
            if os.path.isfile(os.path.join(jdkbasedir, 'javac')):
                jdk = os.path.join(jdkbasedir, 'javac')
            if os.path.isfile(os.path.join(jdkbasedir, 'jar')):
                jarcmd = os.path.join(jdkbasedir, 'jar')
            if os.path.isfile(os.path.join(jdkbasedir, 'java')):
                jre = os.path.join(jdkbasedir, 'java')

        if not jdk:
            if checkcmdinpath('javac'):
                jdk = getcmdpath('javac')
        if not jarcmd:
            if checkcmdinpath('jar'):
                jarcmd = getcmdpath('jar')
        if not jre:
            if checkcmdinpath('java'):
                jre = getcmdpath('java')

        return (jdk, jre, jarcmd)


    @staticmethod
    def run_and_parse_classpath(cmd=None):

        """ Find all jars listed by a cli's classpath subcommand """

        dirs = []
        jars = []

        rc, so, se = run_command(cmd, checkrc=False)

        if rc != 0:
            return (dirs, jars)

        # Split and iterate each path
        paths = [x.strip() for x in so.split(':') if x.strip()]
        for idp,path in enumerate(paths):
            # fix mapr 5.x sandbox classpath problems ...
            if ' ' in path:
                paths += [x.strip() for x in path.split() if x.strip()]
                paths[idp] = ''

        for path in paths:
            if not path:
                continue
            if '*' in path:
                files = glob.glob(path)
                for file in files:
                    if file.endswith(".jar"):
                        jars.append(file)
            elif path.endswith('.jar'):
                jars.append(path)
            elif not path.endswith('.jar'):
                dirs.append(path)

        return (dirs, jars)

    @staticmethod
    def dedupejars_by_checksum(jarlist):

        ''' delete duplicate jars by md5sum '''

        md5cmd = getcmdpath('md5sum')
        jardict = {}

        # Include the real paths for each jar
        for idx,x in enumerate(jarlist):
            xrp = os.path.realpath(x)
            if xrp != x:
                jarlist.append(xrp)

        for x in jarlist:

            cmd = "%s %s | awk '{print $1}'" % (md5cmd, x)
            (rc, so, se) = run_command(cmd, checkrc=False)
            md5 = so.strip()
            if md5 not in jardict:
                jardict[md5] = []
            jardict[md5].append(x)

        for k, v in jardict.iteritems():
            if len(v) == 1:
                continue

            # find the longest basename and filepath
            longest_bn = None

            for idj,jf in enumerate(v):
                jf_basename = os.path.basename(jf)

                if not longest_bn:
                    longest_bn = jf_basename
                else:
                    if len(jf_basename) > len(longest_bn):
                        longest_bn = jf_basename

            # Narrow down by basename ...
            jardict[k] = [x for x in v if os.path.basename(x) == longest_bn]
            if len(jardict[k]) > 1:
                # Narrow down by longest filepath ...
                jardict[k] = [sorted(set(jardict[k]))[-1]]

        out_cp = []
        for k,v in jardict.iteritems():
            out_cp.append(v[0])
        return out_cp

    @staticmethod
    def split_jar_name_and_version(jarname):

        # jetty-util-6.1.26.cloudera.4.p
        # parquet-scala_2.10.jar
        # ('servlet-api', 'servlet-api')

        name = None
        version = ''
        ints = xrange(0,9)
        ints = [str(x) for x in ints]

        delimiter = '-'
        if '_' in jarname:
            delimiter = '_'

        jarname = jarname.replace('.jar', '')
        parts = jarname.split(delimiter)

        if len(parts) == 1:
            name = parts[0]
            version = ''
        else:
            names = []
            version_idx = None
            for idx,x in enumerate(parts):
                if x[0] not in ints:
                    names.append(x)
                else:
                    version_idx = idx
                    break

            name = delimiter.join(names)
            version = delimiter.join(parts[version_idx:])
            if version.endswith('-tests'):
                version = version.replace('-tests', '')
                name += "-tests"

        return (name, delimiter, version)

    @staticmethod
    def filter_jars_by_hadoop_classpath(inclasspath, hcp_jars=None, verbose=False):

        ''' Remove duplicates from a classpath and
            prefer jars from the hadoop classpath  '''

        # Get and dedupe the list of jars in the hadoop classpath (or whatever was passed in for hcp_jars)
        if not hcp_jars:
            hcp_jars = hadoopclasspathcmd()
        hcp_jars = Tracer.dedupejars_by_checksum(hcp_jars)

        # Iterate each jar and get the name|version
        hcp_versions = []
        for idx,x in enumerate(hcp_jars):
            xbn = os.path.basename(x)
            (xname, xdelimiter, xversion) = Tracer.split_jar_name_and_version(xbn)
            if xversion:
                hcp_versions.append((xname, xdelimiter, xversion, x))
            else:
                # Check the real path for a versioned jar filename
                xrp = os.path.realpath(x)
                (xname, xdelimiter, xversion) = Tracer.split_jar_name_and_version(xrp)
                hcp_versions.append((xname, xdelimiter, xversion, xrp))
                hcp_jars[idx] = xrp

        # Get a list of basenames for comparison ...
        hcp_basenames = sorted(set(os.path.basename(x) for x in hcp_jars))

        # Make a list from the input jars
        if type(inclasspath) != list:
            inclasspath = [x for x in inclasspath.split(':') if x]

        # Convert globs to jars
        in_jars = []
        for x in inclasspath:
            if x.endswith('*'):
                xjars = glob.glob(x + '.jar')
                in_jars += xjars
            elif x.endswith('.jar'):
                in_jars.append(x)
        in_basenames = sorted(set(os.path.basename(x) for x in in_jars))

        # Mark any jars that should be removed
        to_delete = []
        for x in in_basenames:
            if x in hcp_basenames:
                continue
            else:
                (xname, xdelimiter, xversion) = Tracer.split_jar_name_and_version(x)
                for hcpv in hcp_versions:
                    if hcpv[0] == xname:
                        if xversion != hcpv[2]:
                            to_delete.append((xname, xdelimiter, xversion))

        to_delete = sorted(set(to_delete))

        # Create a list without the marked jars
        out_jars = in_jars
        for td in to_delete:
            if td[1] != '':
                bn = td[1].join([td[0], td[2]]) + '.jar'
            else:
                bn = ''.join([td[0], td[2]]) + '.jar'
            out_jars = [x for x in out_jars if os.path.basename(x) != bn]

            # add the hcp jar if none remains ...
            for hcp_v in hcp_versions:
                if hcp_v[0] == td[0]:
                    if hcp_v[3] not in out_jars:
                        if verbose:
                            LOG.info("Replacing %s with %s because of hadoop cp filter" \
                                % (bn + '.jar', hcp_v[3]))
                        out_jars.append(hcp_v[3])

        return out_jars

    @staticmethod
    def filter_jars_by_inclasspath(injars, filter=[]):
        ''' Mask a list of jars by a list of filter jars '''

        if type(filter) != list:
            filter = [x for x in filter.split(':') if x]
        outjars = Tracer.filter_jars_by_hadoop_classpath(injars, hcp_jars=filter, verbose=False)
        #import pdb; pdb.set_trace()
        return outjars


    @staticmethod
    def gethivesetv(detectbeeline=True, log=True, workdir=None, options=None):

        jartype = 'hive'
        CLASSPATH = []

        if detectbeeline:
            # huawei's hive command is actually redirected to beeline and doesn't
            # allow handle a normal -e 'set -v' unless beeline is called directly.

            # Need an options object
            class FakeOpts(object):
                verbose = False
            if not options:
                options = FakeOpts()

            # Get absolute path for hive
            hive = getcmdpath('hive')
            beeline = getcmdpath('beeline')

            # Strace first to check what the javacmd is ...
            cmd = "%s -e 'set -v'" % hive
            (rc, so, se) = Tracer._strace(cmd, workdir=workdir, options=options, usetimeout=False)
            JRE, CLASSPATH, JAVACMD, JAVAENV = parse_strace_output(str(so) + str(se))
            if not CLASSPATH:
                CLASSPATH = []

            if not JAVACMD:
                JAVACMD = []

            # Iterate through javacmd args and check if hive or beeline was used ...
            for arg in JAVACMD:
                if arg.endswith('.jar') and 'beeline' in os.path.basename(arg).lower():
                    jartype = 'beeline'
                elif arg == 'org.apache.hive.beeline.BeeLine':
                    jartype = 'beeline'

            if log and jartype == 'beeline':
                LOG.warning("beeline is masquerading as %s" % hive)

        # Call beeline directly if used ...
        if jartype == 'beeline':
            '''
            +--------------------------------------------------------------------------------+
            | yarn.resourcemanager.fs.state-store.uri=${hadoop.tmp.dir}/yarn/system/rmstore  |
            +--------------------------------------------------------------------------------+
            |                                                                                |
            +--------------------------------------------------------------------------------+
            | yarn.resourcemanager.ha.automatic-failover.embedded=true                       |
            +--------------------------------------------------------------------------------+
            '''

            cmd = "%s -e 'set -v'" % (beeline)
            p = Popen(cmd, stdout=PIPE, stderr=subprocess.STDOUT, shell=True)
            (so, se) = p.communicate()

            rawdata = str(so) + str(se)
            rawdata = rawdata.replace('|', '')
            rawdata = rawdata.replace('--', '')
            rawlines = rawdata.split('\n')
            rawlines = [x.strip() for x in rawlines if x.strip() and not x.startswith('+')]
            sobak = so
            so = '\n'.join(rawlines)

        # Call hive otherwise ...
        else:
            cmd = "%s -e 'set -v'" % hive
            (rc, so, se) = run_command(cmd, cwd=workdir)

        # Convert data to dictionary ...
        hiveinfo = Tracer.parsehivesetv(so)

        if not 'env' in hiveinfo:
            hiveinfo['env'] = {}
        if not 'CLASSPATH' in hiveinfo['env']:
            hiveinfo['env']['CLASSPATH'] = []

        # Add the traced classpath if beeline was used
        if (detectbeeline and jartype == 'beeline') or (not hiveinfo['env']['CLASSPATH']):
            hiveinfo['env']['CLASSPATH'] = CLASSPATH

        return hiveinfo


    @staticmethod
    def parsehivesetv(rawtxt):
        hiveinfo = {}
        lines = rawtxt.split('\n')
        lines = [x.strip() for x in lines if x.strip()]
        for idx, line in enumerate(lines):
            if not '=' in line and not ':' in line:
                continue

            section = None
            subkey = None
            value = None

            if '=' in line:
                parts = line.split('=', 1)
                if len(parts) == 2:
                    if ':' in parts[0]:
                        hparts = parts[0].split(':', 1)
                        section = hparts[0]
                        subkey = hparts[1]
                        value = parts[1].strip()
                    else:
                        section = parts[0]
                        value = parts[1].strip()

                    if section:
                        if '"' in section:
                            section = section.replace('"', '')

                    if subkey:
                        if '"' in subkey:
                            subkey = subkey.replace('"', '')

                    if subkey:
                        if section not in hiveinfo:
                            hiveinfo[section] = {}
                        hiveinfo[section][subkey] = value
                    else:
                        hiveinfo[section] = value
                if section == "env" and subkey == "CLASSPATH":
                    # iterate through the next few lines and append if
                    # they should be part of this value
                    for x in xrange(1,10):
                        if not '=' in lines[idx+x]:
                            hiveinfo[section][subkey] += ':' + lines[idx+x].strip()
                        else:
                            break
        return hiveinfo


    @staticmethod
    def gethiveclasspath(preferhivelib=True, workdir=None, log=True):
        jars = []
        dirs = []
        if not workdir:
            workdir = WORKDIR
        hiveinfo = collecthiveinfo(workdir=workdir, log=log)

        classpath = hiveinfo.get('env', {}).get('CLASSPATH', [])
        if not classpath:
            # mapr 3.x
            classpath = hiveinfo.get('system', {}).get('java.class.path', [])

        if type(classpath) == str:
            cp = [x.strip() for x in classpath.split(':') if x.strip()]
        elif type(classpath) == list:
            cp = [x.strip() for x in classpath if x.strip()]
        elif type(classpath) == unicode:
            cp = [x.strip() for x in classpath.split(':') if x.strip()]
        else:
            LOG.error("hiveclasspath is empty")
            cp = []

        for idx,x in enumerate(cp):
            if '*' in x:
                tjars = glob.glob(x)
                tjars = [y for y in tjars if y.endswith('.jar')]
                for y in tjars:
                    if y not in jars:
                        jars.append(y)
            elif x.endswith('.jar'):
                if x not in jars:
                    jars.append(x)
            else:
                if x not in dirs:
                    dirs.append(x)

        # Hive's classpath contains many conflicting jar version
        # so make an attempt to narrow that down to the ones that
        # came from the hive lib dir
        if preferhivelib:
            jars = [os.path.realpath(x) for x in jars]
            hivelibjars = [x for x in jars if 'hive/lib' in x]
            jars = Tracer.filter_jars_by_inclasspath(jars, filter=hivelibjars)

        return (dirs, jars)

    @staticmethod
    def filter_jars_by_latest(injars):

        #deduped_jars = Tracer.dedupejars_by_checksum(injars)
        injars = sorted(set([os.path.realpath(x) for x in injars]))
        exclude = []
        jardict = {}
        for x in injars:
            (xname, xdelimiter, xversion) = Tracer.split_jar_name_and_version(os.path.basename(x))
            if xname not in jardict:
                jardict[xname] = {}
            if xversion not in jardict[xname]:
                jardict[xname][xversion] = []
            if x not in jardict[xname][xversion]:
                jardict[xname][xversion].append(x)

        for k,v in jardict.iteritems():
            if len(v.keys()) < 2:
                continue

            latest = sorted(v.keys(), key=lambda x:LooseVersion(x))
            latest = latest[-1]

            for k2, v2 in v.iteritems():
                if k2 != latest:
                    for x in v2:
                        if x not in exclude:
                            exclude.append(x)

        for x in exclude:
            if x in injars:
                LOG.info("[filter:latest] removing %s" % x)
                injars.remove(x)

        return injars

    @staticmethod
    def filter_jars_by_count(injars):

        injars = sorted(set([os.path.realpath(x) for x in injars]))
        exclude = []
        jardict = {}
        for x in injars:
            (xname, xdelimiter, xversion) = Tracer.split_jar_name_and_version(os.path.basename(x))
            if xname not in jardict:
                jardict[xname] = {}
            if xversion not in jardict[xname]:
                jardict[xname][xversion] = []
            if x not in jardict[xname][xversion]:
                jardict[xname][xversion].append(x)

        for k,v in jardict.iteritems():
            if len(v.keys()) < 2:
                continue

            latest = sorted(v.keys(), key=lambda x:LooseVersion(x))
            latest = latest[-1]

            highest_count = None
            for k2, v2 in v.iteritems():
                if not highest_count:
                    highest_count = k2
                    continue
                if len(v2) > len(jardict[k][highest_count]):
                    highest_count = k2

            for k2, v2 in v.iteritems():
                if k2 != highest_count:
                    for x in v2:
                        if x not in exclude:
                            exclude.append(x)

        for x in exclude:
            if x in injars:
                LOG.info("[filter:count] removing %s" % x)
                injars.remove(x)

        return injars

    @staticmethod
    def jrejarfilter(jre, jars):

        # abort if no JRE provided
        if not jre:
            return jars

        # create jre base path
        jrepath = jre.replace('/bin/java', '')

        # filter out jars that contain the path
        tmpjars = []
        for jf in jars:
            if not jrepath in jf and not '/jre/lib/' in jf:
                tmpjars.append(jf)
        return tmpjars


############################################################
#   HIVE HELPER CODE
############################################################

HIVEJDBCCODE = '''
import java.io.PrintWriter;
import java.sql.SQLException;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;
import java.sql.DriverManager;

public class HiveJdbcClient {
  private static String driverName = "org.apache.hive.jdbc.HiveDriver";

  /**
   * @param args
   * @throws SQLException
   */
  public static void main(String[] args) throws SQLException {
    try {
      Class.forName(driverName);
    } catch (ClassNotFoundException e) {
      // TODO Auto-generated catch block
      e.printStackTrace();
      System.exit(1);
    }

    // set logging
    DriverManager.setLogWriter(new PrintWriter(System.out));

    // set login timeout
    DriverManager.setLoginTimeout(10);

    // show all available drivers
    java.util.Enumeration e = DriverManager.getDrivers();
    while (e.hasMoreElements()) {
        Object driverAsObject = e.nextElement();
        System.out.println("DRIVER = " + driverAsObject);
    }

    System.out.println("Creating connection with drivermanager ...");
    Connection con = DriverManager.getConnection(%s);

    System.out.println("Creating statement object from connection ...");
    Statement stmt = con.createStatement();

    System.out.println("Calling show databases ...");
    stmt.execute("show databases");

    System.out.println("Calling show tables ...");
    stmt.execute("show tables");

    System.out.println("Calling create table ...");
    stmt.execute("create table hadooptracertest(a INT)");

    System.out.println("Calling drop table ...");
    stmt.execute("drop table hadooptracertest");
  }
}
'''

HIVEBUILDSCRIPT = '''#!/bin/bash

# write out the java code
cat > HiveJdbcClient.java << EOF
$CODE
EOF

# export the CLASSPATH
$CLASSPATH

# cleanup old binaries
rm -f HiveJdbcClient.class

# compile the java code
$JDK HiveJdbcClient.java

# find the timeout command
TIMEOUT=$$(command -v timeout)

# run the code with -verbose:class
BASECMD="$JRE -verbose:class HiveJdbcClient"
rm -rf hivejava.debug
if [ $$TIMEOUT != '' ]; then
    echo "Running with timeout"
    $$TIMEOUT -s SIGKILL 360s $$BASECMD 2>&1 | tee -a hivejava.debug
else
    echo "Running hive without timeout"
    $$BASECMD 2>&1 | tee -a hivejava.debug
fi

# check the exit code
RC=$$?
if [ $$RC != 0 ]; then
    exit $$RC
fi

# check for any errors
egrep -e ^Error -e ^"java.lang.ClassNotFoundException" hivejava.debug
RC=$$?
if [ $$RC == 0 ]; then
    exit 1
fi
'''

class HiveJdbcTrace(Tracer):

    """ Find Hive jdbc client jars """

    def Run(self):

        if not os.path.isdir(WORKDIR):
            os.makedirs(WORKDIR)
        self.workdir = tempfile.mkdtemp(prefix='hivejdbc.', dir=WORKDIR)
        self.jdkbasedir = locatejdkbasedir()
        self.jdk = None
        if self.jdkbasedir:
            self.jdk = os.path.join(self.jdkbasedir, 'javac')
        self.hivesitexml = None
        self.krb_principal = None
        self.hiveinfo = {}
        self.jdbcparams = None
        self.beeline = getcmdpath('beeline')

        # get classpath from tracing hive
        LOG.info("hivejdbc - trace hive cli")
        #self.hive_show_databases()
        self.hive_trace_version()

        # Use the given principal
        if self.options.hivejdbcurl:
            self.jdbcparams = '"' + self.options.hivejdbcurl + '"'
        else:
           # get the metadata
            LOG.info("hivejdbc - get hive info")
            self.hiveinfo = collecthiveinfo(workdir=WORKDIR)

            LOG.info("hivejdbc - set jdbc principal")
            self.set_principal()

            LOG.info("hivejdbc - set jdbc params")
            self.set_jdbc_params()

        # Build a jar, run it and then parse the -verbose:class output
        LOG.info("hivejdbc - trace jdbc")
        self.hive_jdbc_trace()

        LOG.info("hivejdbc - finished")


    def hive_trace_version(self):

        ''' Get essential info by strace'ing hive --version '''

        hivecmd = getcmdpath('hive')
        cmd = "%s --version" % hivecmd
        (rc, so, se) = Tracer._strace(cmd, options=self.options)
        LOG.info("hivejdbc - strace default hive finished: %s" % rc)
        self.jre, self.classpath, self.javacmd, self.javaenv = \
            parse_strace_output(str(so) + str(so), shorten=False)


    def hive_show_databases(self):

        ''' DEPRECATED [SLOW+BUGGY] '''

        hivecmd = getcmdpath('hive')
        cmd = "%s -e 'show databases'" % hivecmd
        (rc, so, se) = Tracer._strace(cmd, options=self.options)
        LOG.info("hivejdbc - strace default hive finished: %s" % rc)
        self.rc_strace = rc
        if self.rc_strace == 137:
            LOG.error('hivejdbc - (show databases) strace timed out [>%s]' % TIMEOUT)

        # Do not shorten the classpath (DL+derby workaround)
        self.jre, self.classpath, self.javacmd, self.javaenv = \
            parse_strace_output(str(so) + str(so), shorten=False)

        if not self.jre or not self.javacmd:
            LOG.error('hivejdbc - (show databases) found no jre or javacmd in strace')
            return False

        self.sitexmls = parse_strace_open_file(str(so) + str(so), "site.xml", list=True)
        LOG.info("hivejdbc - site.xmls %s" % (self.sitexmls))
        if self.sitexmls:
            for sx in self.sitexmls:
                if sx.endswith('hive-site.xml'):
                    self.hivesitexml = sx

        # If we don't have a hive-site.xml, write out debug logs
        if not self.hivesitexml or self.options.noclean:
            fn = os.path.join(self.workdir, "hive.strace")
            f = open(fn, "wb")
            f.write('rc:%s\n' % self.rc_strace)
            f.write(str(so) + str(so))
            f.close()

        if self.javacmd:
            LOG.info("hivejdbc (show databases) - [-verbose:class]")
            vrc, rawdataj = javaverbose(self.options, self.classpath, self.javacmd,
                                        self.javaenv, svckey='hivejdbc')

            LOG.info("hivejdbc (show databases) - parse jars paths")
            self.classpaths = parseverboseoutput(rawdataj)
            self.jars = classpathstojars(self.classpaths)
            self.jars = Tracer.jrejarfilter(self.jre, self.jars)
            if self.options.excludepackage and not self.options.noexclusions:
                tmpjars = exclude_packages(':'.join(self.jars), self.options.excludepackage)
                self.jars = [x for x in tmpjars.split(':') if x]

    def set_jdbc_params(self):
        # https://cwiki.apache.org/confluence/display/Hive/Setting+Up+HiveServer2
        # Options are NONE, NOSASL, KERBEROS, LDAP, PAM and CUSTOM.
        # http://www-01.ibm.com/support/knowledgecenter/SSPT3X_3.0.0/com.ibm.swg.im.infosphere.biginsights.admin.doc/doc/bi_admin_enable_hive_authorization.html
        # "jdbc:hive2://%s:10000/default%s", "hive", ""
        # hive.server2.authentication=NONE
        # hive.server2.authentication=CUSTOM
        # hive.server2.custom.authentication.class=org.apache.hive.service.auth.WebConsoleAuthenticationProviderIm
        # hive.server2.ssl=false
        # hive.server2.enable.doAs=true
        # hive.server2.enable.impersonation=true
        # hive.server2.thrift.port=10000
        # hive.server2.thrift.bind.host=greysky1.unx.sas.com

        if self.options.hivejdbcurl:
            self.jdbcparams = self.options.hivejdbcurl
            return True

        authtype = False
        thriftport = 10000
        thrifthost = 'localhost'
        usessl = False

        if 'hive.server2.authentication' in self.hiveinfo:
            atype = self.hiveinfo['hive.server2.authentication']
            if atype.upper() == 'NONE':
                authtype = 'NONE'
            elif atype.upper() == 'KERBEROS':
                authtype = 'KERBEROS'
            elif atype.upper() == 'CUSTOM':
                if 'hive.server2.custom.authentication.class' in self.hiveinfo:
                    # settings for ibm biginsight
                    tt = self.hiveinfo['hive.server2.custom.authentication.class']
                    if tt == 'org.apache.hive.service.auth.WebConsoleAuthenticationProviderIm':
                        authtype = 'PLAIN'
                    elif tt == 'org.apache.hive.service.auth.WebConsoleAuthenticationProviderImpl':
                        authtype = 'PLAIN'

        if 'hive.server2.ssl' in self.hiveinfo:
            usessl = self.hiveinfo['hive.server2.ssl']

        if usessl:
            LOG.info("hivejdbc - usessl: %s" % usessl)

        if 'hive.metastore.uris' in self.hiveinfo:
            # fgrep dmmlax15 hivesettings.txt
            #   hive.metastore.uris=thrift://dmmlax15.unx.sas.com:9083
            pass

        if 'hive.server2.thrift.port' in self.hiveinfo:
            thriftport = int(self.hiveinfo['hive.server2.thrift.port'])

        if 'hive.server2.thrift.bind.host' in self.hiveinfo:
            thrifthost = self.hiveinfo['hive.server2.thrift.bind.host']

        """
        self.hiveinfo['hive.server2.enable.doAs']
        self.hiveinfo['hive.server2.enable.impersonation']
        """

        # catchall for unknown configurations ...
        if not self.options.hivehost and not thrifthost:
            thrifthost = 'localhost'

        # "jdbc:hive2://%s:10000/default%s", "hive", ""
        LOG.info('hivejdbc - authtype: %s' % authtype)
        if self.options.hivehost:
            params = ['jdbc:hive2://%s:%s/default' % (self.options.hivehost, thriftport)]
        else:
            params = ['jdbc:hive2://%s:%s/default' % (thrifthost, thriftport)]
        if authtype == 'KERBEROS':
            if not self.krb_principal:
                if 'hive.server2.authentication.kerberos.principal' in self.hiveinfo:
                    self.krb_principal = \
                        self.hiveinfo['hive.server2.authentication.kerberos.principal']
            # the host var has to be replaced for the principal to work
            if '_HOST' in self.krb_principal:
               self.krb_principal = self.krb_principal.replace('_HOST',
                                                    socket.gethostname())
            params[0] += ';principal=' + self.krb_principal
        elif authtype == 'PLAIN':
            params.append('%s' % self.options.hiveusername)
            #FIXME ... password
            if not self.options.hivepassword:
                LOG.error("hivejdbc - hive password was set to null but is required")
            params.append("%s" % self.options.hivepassword)
        elif authtype == 'NONE':
            # mapr sandbox
            params.append('%s' % self.options.hiveusername)
            params.append('%s' % self.options.hivepassword)

        LOG.info("hivejdbc - jdbc parameters: %s" % params)
        self.metadata['connection_params'] = params
        self.jdbcparams = ''
        plen = len(params) - 1
        for idx, val in enumerate(params):
            self.jdbcparams += '"%s"' % val
            if idx != plen:
                self.jdbcparams += ", "


    def set_principal(self):
        if self.options:
            if hasattr(self.options, "hivehost"):
                if not self.options.hivehost and not 'hive.server2.thrift.bind.host' in self.hiveinfo:
                    LOG.warning("hivejdbc - Hive hostname was set to Null")

        if not hasattr(self, 'hivesitexml'):
            self.hivesitexml = None
        if not self.hivesitexml:
            LOG.info('hivejdbc - no hive sitexml found')
        elif self.hivesitexml:
            # get the authenication details
            f = open(self.hivesitexml)
            rawxml = f.read()
            f.close()

            # auth enabled?
            self.auth_enabled = xmlnametovalue(rawxml, "hive.security.authorization.enabled")
            self.krb_principal = xmlnametovalue(rawxml, "hive.server2.authentication.kerberos.principal")

            # use kerberos or not?
            if not self.auth_enabled:
                LOG.info('hivejdbc - auth not enabled')
            elif self.auth_enabled:
                LOG.info('hivejdbc - auth enabled')
                if ast.literal_eval(self.auth_enabled.title()):
                    # fix the url
                    if self.krb_principal:
                        if "_HOST" in self.krb_principal:
                            hname = socket.getfqdn()
                            self.krb_principal = self.krb_principal.replace('_HOST', hname)
                            self.hostname = hname
                            LOG.info("hive - principal set to %s" % (self.krb_principal))

        #   hive/_HOST@UNX.SAS.COM
        #   jdbc:hive2://$FQDN:10000/default;principal=hive/$FQDN@UNX.SAS.COM

        # Fix the principal string
        if not hasattr(self, 'krb_principal'):
            self.krb_principal = None
        if not self.krb_principal:
            self.krb_principal = ''
        else:
            if not self.krb_principal.startswith(';principal='):
                self.krb_principal = ';principal=' + self.krb_principal

        LOG.info('hivejdbc - krb principal: %s' % self.krb_principal)


    def hive_jdbc_trace(self):

        # Can not proceed if there is not a jdk
        if not self.jdk:
            if checkcmdinpath('javac'):
                self.jdk = os.path.realpath(getcmdpath('javac'))
            else:
                LOG.error("hivejdbc - no javac command found to compile java")
                return False

        '''
        # Prioritize the classpath from hiveinfo [truncated on huawei]
        if self.hiveinfo.get('env', None):
            if self.hiveinfo['env'].get('CLASSPATH', None):
                self.classpath = self.hiveinfo['env']['CLASSPATH']
        '''

        # Remove excluded packages (DL+derby workaround)
        if self.options.excludepackage and not self.options.noexclusions:
            self.jdbc_classpath = exclude_packages(self.classpath, self.options.excludepackage)
        else:
            self.jdbc_classpath = self.classpath

        # Chop up the classpath into multiple lines to avoid max command lengths
        LOG.info("hivejdbc - creating verbose build script")
        BASHCP = ""
        CPS = [x for x in self.jdbc_classpath.split(':') if x]
        for idx,x in enumerate(CPS):
            if idx == 0:
                BASHCP = 'export CLASSPATH=".:%s"\n' % x
            else:
                BASHCP += 'export CLASSPATH="$CLASSPATH:%s"\n' % x

        # Substitute, replace and create the final buildscript
        HIVEJDBCPGM = HIVEJDBCCODE % (self.jdbcparams)
        ddict = {'CODE': HIVEJDBCPGM, 'CLASSPATH': BASHCP, 'JDK': self.jdk, 'JRE': self.jre}
        s = Template(HIVEBUILDSCRIPT)
        bs = s.substitute(ddict)

        # write out buildscript
        makefile = os.path.join(self.workdir, "makefile")
        LOG.info("hivejdbc - makefile: %s" % makefile)
        fh = open(makefile, "wb")
        fh.write(bs)
        fh.close()

        # run buildscript
        args = "/bin/bash %s" % makefile
        p = Popen(args, cwd=self.workdir, stdout=PIPE, stderr=PIPE, shell=True)
        so, se = p.communicate()
        rc = p.returncode
        self.rc_verbose = rc
        self.rc_strace = rc

        if rc != 0:
            lines = str(so) + str(se)
            lines = [x for x in lines.split('\n') if x.strip()]
            lines = [x for x in lines if not 'loaded' in x.lower()]
            lines = [x for x in lines if not 'opened' in x.lower()]
            for x in lines:
                LOG.error("hivejdbc - %s" % x)

        # The classloader output is in the hivejava.debug file
        debugfile = os.path.join(self.workdir, "hivejava.debug")
        if not os.path.isfile(debugfile):
            return None, None
        else:
            LOG.info("hivejdbc - %s" % debugfile)

        LOG.info('hivejdbc - parsing verbose log')

        f = open(debugfile, "rb")
        data = f.read()
        f.close()

        if not self.classpaths:
            self.classpaths = []
        classpaths = parseverboseoutput(data)
        if classpaths:
            for cp in classpaths:
                if cp not in self.classpaths:
                    self.classpaths.append(cp)

            self.jars = classpathstojars(classpaths)
            if self.jars:
                self.jars = [x for x in Tracer.jrejarfilter(self.jre, self.jars)]

        self.jarfiles = self.jars
        LOG.info('hivejdbc - total jars: %s' % len(self.jars))


############################################################
#   MAPREDUCE HELPER CODE
############################################################

WC = """/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* http://svn.apache.org/viewvc/hadoop/common/trunk/hadoop-mapreduce-project/hadoop-mapreduce-examples/
        src/main/java/org/apache/hadoop/examples/WordCount.java?view=co */

package org.apache.hadoop.examples;

import java.io.IOException;
import java.util.StringTokenizer;

import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.fs.Path;
import org.apache.hadoop.io.IntWritable;
import org.apache.hadoop.io.Text;
import org.apache.hadoop.mapreduce.Job;
import org.apache.hadoop.mapreduce.Mapper;
import org.apache.hadoop.mapreduce.Reducer;
import org.apache.hadoop.mapreduce.lib.input.FileInputFormat;
import org.apache.hadoop.mapreduce.lib.output.FileOutputFormat;
import org.apache.hadoop.util.GenericOptionsParser;

public class WordCount {

  public static class TokenizerMapper
       extends Mapper<Object, Text, Text, IntWritable>{

    private final static IntWritable one = new IntWritable(1);
    private Text word = new Text();

    public void map(Object key, Text value, Context context
                    ) throws IOException, InterruptedException {
      StringTokenizer itr = new StringTokenizer(value.toString());
      while (itr.hasMoreTokens()) {
        word.set(itr.nextToken());
        context.write(word, one);
      }
    }
  }

  public static class IntSumReducer
       extends Reducer<Text,IntWritable,Text,IntWritable> {
    private IntWritable result = new IntWritable();

    public void reduce(Text key, Iterable<IntWritable> values,
                       Context context
                       ) throws IOException, InterruptedException {
      int sum = 0;
      for (IntWritable val : values) {
        sum += val.get();
      }
      result.set(sum);
      context.write(key, result);
    }
  }

  public static void main(String[] args) throws Exception {
    Configuration conf = new Configuration();
    String[] otherArgs = new GenericOptionsParser(conf, args).getRemainingArgs();
    if (otherArgs.length < 2) {
      System.err.println("Usage: wordcount <in> [<in>...] <out>");
      System.exit(2);
    }
    Job job = new Job(conf, "word count");
    job.setJarByClass(WordCount.class);
    job.setMapperClass(TokenizerMapper.class);
    job.setCombinerClass(IntSumReducer.class);
    job.setReducerClass(IntSumReducer.class);
    job.setOutputKeyClass(Text.class);
    job.setOutputValueClass(IntWritable.class);
    for (int i = 0; i < otherArgs.length - 1; ++i) {
      FileInputFormat.addInputPath(job, new Path(otherArgs[i]));
    }
    FileOutputFormat.setOutputPath(job,
      new Path(otherArgs[otherArgs.length - 1]));
    System.exit(job.waitForCompletion(true) ? 0 : 1);
  }
}
"""


class MapReduceTrace(Tracer):
    """ Create and strace a mapreduce job. """

    def Run(self):
        if not os.path.isdir(WORKDIR):
            os.makedirs(WORKDIR)
        self.workdir = tempfile.mkdtemp(prefix='mapreduce.', dir=WORKDIR)
        self.jdkbasedir = None
        self.jdk = None
        self.jarcmd = None
        self.jarfile = None

        self.wc_code = WC

        #def getcmdpath(cmd):
        #def checkcmdinpath(cmd):

        # setup necessary java tools
        self.jdkbasedir = locatejdkbasedir()
        if self.jdkbasedir:
            if os.path.isfile(os.path.join(self.jdkbasedir, 'javac')):
                self.jdk = os.path.join(self.jdkbasedir, 'javac')
            if os.path.isfile(os.path.join(self.jdkbasedir, 'jar')):
                self.jarcmd = os.path.join(self.jdkbasedir, 'jar')

        if not self.jdk:
            if checkcmdinpath('javac'):
                self.jdk = getcmdpath('javac')
        if not self.jarcmd:
            if checkcmdinpath('jar'):
                self.jarcmd = getcmdpath('jar')

        if not self.jdk or not self.jarcmd:
            LOG.error("mapreduce - no javac or jar commands found to compile jar")
            return False

        # build the code
        compiled = self.compilejava()
        if not compiled:
            LOG.error("mapreduce - jar compile failed")
            return False

        # run the code with strace
        self.runmapreduce()

        self.jarfiles = self.jars

        # cleanup
        LOG.info("mapreduce - finished")

    def compilejava(self):
        # compile the wordcount code

        # write out the java file
        jfile = os.path.join(self.workdir, "WordCount.java")
        f = open(jfile, "wb")
        f.write(self.wc_code)
        f.close()

        # compile the code
        classdir = os.path.join(self.workdir, "wordcount_classes")
        os.makedirs(classdir)
        makefile = os.path.join(self.workdir, 'build.sh')
        f = open(makefile, "wb")
        f.write("#!/bin/bash\n")
        f.write("CLASSPATH=$(hadoop classpath)\n")
        f.write("%s -cp $CLASSPATH -d wordcount_classes WordCount.java\n" % self.jdk)
        f.write("RC=$?\n")
        f.write("if [ $RC != 0 ]; then\n")
        f.write("   exit $RC\n")
        f.write("fi\n")
        f.write("%s -cvf wordcount.jar -C wordcount_classes/ .\n" % self.jarcmd)
        f.write("RC=$?\n")
        f.write("if [ $RC != 0 ]; then\n")
        f.write("   exit $RC\n")
        f.write("fi\n")
        f.close()

        LOG.info("mapreduce - compile wordcount.jar")
        cmd = "bash -x build.sh"
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        if rc != 0:
            data = [x for x in (str(so) + str(se)).split("\n") if x]
            for x in data:
                LOG.error("mapreduce - %s" % x)
            return False
        return True

    def runmapreduce(self):
        # run the jar with hadoop|hdfs jar command

        # the user needs a writeable homedir
        #   Caused by: org.apache.hadoop.ipc.RemoteException
        #    (org.apache.hadoop.security.AccessControlException):
        #    Permission denied: user=root, access=WRITE,
        #       inode="/user":hdfs:supergroup:drwxr-xr-x

        # make two unique tmpdirs in hdfs
        tdir1 = tempfile.mkdtemp(prefix='%s-wordcount1' % getpass.getuser(), dir='/tmp')
        shutil.rmtree(tdir1)
        tdir2 = tempfile.mkdtemp(prefix='%s-wordcount2' % getpass.getuser(), dir='/tmp')
        shutil.rmtree(tdir2)

        tlog = open("%s/test.log" % self.workdir, "wb")

        # Assemble fake data
        f = open("%s/file0" % self.workdir, "wb")
        f.write('Hello World Bye World\n')
        f.close()
        f = open("%s/file1" % self.workdir, "wb")
        f.write('Hello Hadoop Goodbye Hadoop\n')
        f.close()

        cmd = 'hadoop fs -mkdir %s\n' % tdir1
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        tlog.write('%s\n' % rc)
        cmd = 'hadoop fs -mkdir %s\n' % tdir2
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        tlog.write('%s\n' % rc)

        cmd = 'hadoop fs -mkdir %s/input\n' % tdir1
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        tlog.write('%s\n' % rc)
        cmd = 'hadoop fs -mkdir %s/input\n' % tdir2
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        tlog.write('%s\n' % rc)

        # Put fake data into hdfs
        cmd = 'hadoop fs -put %s/file* %s/input\n' % (self.workdir, tdir1)
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        tlog.write('%s\n' % rc)
        cmd = 'hadoop fs -put %s/file* %s/input\n' % (self.workdir, tdir2)
        (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
        tlog.write('%s\n' % rc)
        tlog.close()

        # wait for src2 to get created [prone to race conditions]
        rc = 1
        retries = 5
        while rc != 0 and retries > 0:
            LOG.info("mapreduce - waiting for %s/input creation" % tdir2)
            time.sleep(2)
            cmd = 'hadoop fs -ls %s/input\n' % tdir2
            (rc, so, se) = run_command(cmd, cwd=self.workdir, checkrc=False)
            retries -= 1

        # run it once to get the full java command
        LOG.info("mapreduce - hadoop jar wordcount.jar")
        cmd = 'hadoop jar %s/wordcount.jar org.apache.hadoop.examples.WordCount' % self.workdir
        cmd += ' %s/input %s/output' % (tdir1, tdir1)
        (rc2, so2, se2) = Tracer._strace(cmd, options=self.options)
        self.rc_strace = rc2

        JRE, CLASSPATH, JAVACMD, JAVAENV = parse_strace_output(str(so2) + str(so2))
        self.classpath = CLASSPATH
        self.sitexmls = parse_strace_open_file(str(so2) + str(so2), "site.xml", list=True)

        if JAVACMD:

            # alter the destination to avoid delays / race conditions
            s1 = "%s/input" % tdir1
            s2 = "%s/input" % tdir2
            d1 = "%s/output" % tdir1
            d2 = "%s/output" % tdir2
            for idx, val in enumerate(JAVACMD):
                if val == s1:
                    JAVACMD[idx] = s2
                if val == d1:
                    JAVACMD[idx] = d2

            # run it again to get the verbose classloader output
            LOG.info("mapreduce [-verbose:class]")
            vrc, rawdataj = javaverbose(self.options, CLASSPATH, JAVACMD, JAVAENV, svckey='mapreduce')
            self.rc_verbose = vrc

            if vrc != 0:
                lines = [x.strip().replace('\t', '') for x in rawdataj.split('\n') if x]
                #import pdb; pdb.set_trace()
                for line in lines:
                    if line.startswith('Exception'):
                        LOG.error("mapreduce - %s" % line)

            LOG.info("mapreduce - parse jars paths")
            self.classpaths = parseverboseoutput(rawdataj)
            jars = classpathstojars(self.classpaths)
            self.jars = Tracer.jrejarfilter(JRE, jars)

        # Cleanup
        cmd = "hadoop fs -rm -f -R -skipTrash %s" % tdir1
        run_command(cmd, cwd=self.workdir, checkrc=False)
        cmd = "hadoop fs -rm -f -R -skipTrash %s" % tdir2
        run_command(cmd, cwd=self.workdir, checkrc=False)


############################################################
#   HCATALOG HELPER CODE
############################################################

'''
HDP 2.1
hive-hcatalog-core-0.13.0.2.1.5.0-695.jar
hive-webhcat-java-client-0.13.0.2.1.5.0-695.jar
jdo-api-3.0.1.jar
jersey-client-1.9.jar
libthrift-0.9.0.jar

For CDH 5.1:
hive-hcatalog-core-0.12.0-cdh5.1.2.jar
hive-webhcat-java-client-0.12.0-cdh5.1.2.jar
jdo-api-3.0.1.jar
libthrift-0.9.0.cloudera.2.jar
parquet-hadoop-bundle.jar
'''
# https://github.com/apache/hcatalog/blob/trunk/src/test/e2e/hcatalog/tests/hcat.conf
HC = """
drop table if exists TracerHcatTest;
create table TracerHcatTest (name string,id int) row format delimited fields terminated by ':' stored as textfile;
describe TracerHcatTest;
drop table TracerHcatTest;
"""


class HcatalogTrace(Tracer):
    """ Create and strace an hcatalog job. """

    # http://hortonworks.com/kb/working-with-files-in-hcatalog-tables/

    def Run(self):
        if not os.path.isdir(WORKDIR):
            os.makedirs(WORKDIR)
        self.workdir = tempfile.mkdtemp(prefix='hcatalog.', dir=WORKDIR)

        self.TraceHcatCLI()
        if type(self.jars) == list:
            LOG.info("hcatalog - total jars: %s" % len(self.jars))
        else:
            LOG.info("hcatalog - total jars: 0")
        LOG.info("hcatalog - finished")


    def TraceHcatCLI(self):
        # org.apache.hive.hcatalog.cli.HCatCli

        self.hc_code = HC

        # write test ddl code to a file
        fdest = os.path.join(self.workdir, "test.hcatalog")
        f = open(fdest, "wb")
        f.write(self.hc_code)
        f.close()

        hcat = self.cmddict.get('hcat', 'hcat')
        cmd = "%s -f %s" % (hcat, fdest)
        (rc, so, se) = Tracer._strace(cmd, options=self.options)

        if rc != 0:
            data = str(so) + str(se)
            data = data.split('\n')
            for line in [x for x in data if x]:
                if "error" in line.lower() \
                    or "failed:" in line.lower() \
                    or "exception" in line.lower() \
                    or "refused" in line.lower():

                    if not "loaded " in line.lower():
                        LOG.error("thrift - %s" % line.strip())

        self.rc_strace = rc
        self.jre, self.classpath, self.javacmd, self.javaenv = \
                                parse_strace_output(str(so) + str(so))

        self.sitexmls = parse_strace_open_file(str(so) + str(so), "site.xml", list=True)

        if self.javacmd:

            # Remove excluded packages (DL+derby workaround)
            if self.options.excludepackage and not self.options.noexclusions:
                self.classpath = exclude_packages(self.classpath,
                                            self.options.excludepackage)
            else:
                self.classpath = self.classpath

            LOG.info("hcatalog [-verbose:class]")
            vrc, rawdataj = javaverbose(self.options, self.classpath, self.javacmd, self.javaenv, svckey='hcatalog')
            self.rc_verbose = vrc

            LOG.info("hcatalog - parse jars paths")
            self.classpaths = parseverboseoutput(rawdataj)
            self.jars = classpathstojars(self.classpaths)
            self.jars = Tracer.jrejarfilter(self.jre, self.jars)
            self.jarfiles = self.jars



############################################################
#   HCAT API HELPER CODE
############################################################

HCAPI = """/*
* HCAT API script
*/
package org.hts.hcat;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.hive.conf.HiveConf;
import org.apache.hadoop.hive.metastore.HiveMetaStore;
import org.apache.hadoop.hive.metastore.api.PartitionEventType;
//import org.apache.hadoop.hive.ql.WindowsPathUtil;
import org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat;
import org.apache.hadoop.hive.ql.io.RCFileInputFormat;
import org.apache.hadoop.hive.ql.io.RCFileOutputFormat;
import org.apache.hadoop.hive.ql.io.orc.OrcInputFormat;
import org.apache.hadoop.hive.ql.io.orc.OrcOutputFormat;
import org.apache.hadoop.hive.ql.io.orc.OrcSerde;
import org.apache.hadoop.hive.ql.metadata.Table;
import org.apache.hadoop.hive.serde.serdeConstants;
import org.apache.hadoop.hive.serde2.columnar.LazyBinaryColumnarSerDe;
import org.apache.hadoop.mapred.TextInputFormat;
import org.apache.hive.hcatalog.cli.SemanticAnalysis.HCatSemanticAnalyzer;
import org.apache.hive.hcatalog.common.HCatConstants;
import org.apache.hive.hcatalog.common.HCatException;
import org.apache.hive.hcatalog.data.schema.HCatFieldSchema;
import org.apache.hive.hcatalog.data.schema.HCatFieldSchema.Type;
//import org.apache.hive.hcatalog.NoExitSecurityManager;

//import org.junit.AfterClass;
//import org.junit.BeforeClass;
//import org.junit.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

//import static org.junit.Assert.assertNotNull;
//import static org.junit.Assert.fail;
//import static org.junit.Assert.assertEquals;
//import static org.junit.Assert.assertFalse;
//import static org.junit.Assert.assertTrue;
//import static org.junit.Assert.assertArrayEquals;

import org.apache.hadoop.util.Shell;

//import org.apache.hcatalog.api.HCatClient;
import org.apache.hive.hcatalog.api.HCatClient;
import org.apache.hive.hcatalog.api.HCatTable;
import org.apache.hive.hcatalog.api.HCatCreateTableDesc;

public class TestHCatClient {
  private static final Logger LOG = LoggerFactory.getLogger(TestHCatClient.class);
  //private static final String msPort = "20101";
  private static final String msPort = "9083";
  private static HiveConf hcatConf;
  private static boolean isReplicationTargetHCatRunning = false;
  //private static final String replicationTargetHCatPort = "20102";
  private static final String replicationTargetHCatPort = "9083";
  private static HiveConf replicationTargetHCatConf;
  private static SecurityManager securityManager;

  public static void main(String[] args) throws Exception {

    // Create the config
    hcatConf = new HiveConf(TestHCatClient.class);
    //hcatConf.setVar(HiveConf.ConfVars.METASTOREURIS, "thrift://localhost:"
    //  + msPort);
    hcatConf.setVar(HiveConf.ConfVars.METASTOREURIS, "$thrift_uri");
    hcatConf.setIntVar(HiveConf.ConfVars.METASTORETHRIFTCONNECTIONRETRIES, 3);
    hcatConf.setIntVar(HiveConf.ConfVars.METASTORETHRIFTFAILURERETRIES, 3);
    System.out.println(hcatConf);

    Boolean connected = false;
    HCatClient client = null;

    try {
        // Create the client object
        client = HCatClient.create(new Configuration(hcatConf));
        connected = true;
    } catch (Exception e) {
        //System.out.println(e.getMessage());
        e.printStackTrace();
        connected = false;
    }

    if ( connected ) {
        // Show databases
        List<String> dbNames = client.listDatabaseNamesByPattern("*");
        System.out.println(dbNames);

        // Show tables
        String dbName = "default";
        List<String> tbNames = client.listTableNamesByPattern(dbName, "*");
        System.out.println(tbNames);

        // Kill client and forcefully exit
        client.close();

        System.exit(0);

    } else {
        System.exit(1);
    }

  }

}
"""


class HcatAPITrace(Tracer):
    """ Create and strace an hcatalog API job. """

    # Primary target:
    #   * hive-webhcat-java-client-0.13.0.2.1.5.0-695.jar

    def Run(self):
        if not os.path.isdir(WORKDIR):
            os.makedirs(WORKDIR)
        self.workdir = tempfile.mkdtemp(prefix='hcatapi.', dir=WORKDIR)

        # get hive config data
        #self.hiveclass = HiveJdbcTrace()
        #self.hiveclass.collecthiveinfo()
        LOG.info("hcatapi - fetching hive config")
        self.hiveinfo = collecthiveinfo(workdir=WORKDIR)
        #LOG.info("hcatapi - hiveinfo: %s" % self.hiveinfo)

        # What is the thrift URI ?
        self.thrifturi = None
        if self.hiveinfo:
            if "hive.metastore.uris" in self.hiveinfo:
                if type(self.hiveinfo['hive.metastore.uris']) == list:
                    self.thrifturi = self.hiveinfo['hive.metastore.uris'][0]
                else:
                    self.thrifturi = self.hiveinfo['hive.metastore.uris']

            else:
                # EMR
                if ThriftTrace.checkthriftport():
                    self.thrifturi = "thrift://localhost:9083"
                else:
                    LOG.error("hcatapi - no hive.metastore.uris in hiveinfo")
                    LOG.error("hcatapi - hiveinfo: %s" % self.hiveinfo)

        if self.thrifturi:
            LOG.info("hcatapi - thrifturi: %s" % self.thrifturi)
        else:
            LOG.error("hcatapi - thrifturi: %s" % self.thrifturi)

        # What is the jre/jdk path?
        # system:sun.boot.class.path=/usr/java/jdk1.7.0_67-cloudera/...:...:
        self.jre, self.jdk, self.jar = self.locateJREandJDK()
        #import pdb; pdb.set_trace()

        # What is the hive classpath?
        # system:java.class.path=/etc/hadoop/conf
        self.hclasspath = None
        hive_jars = []
        if self.hiveinfo:
            if 'system' in self.hiveinfo:
                if 'java.class.path' in self.hiveinfo['system']:
                    self.hclasspath = self.hiveinfo['system']['java.class.path']
                    for x in self.hiveinfo['system']['java.class.path'].split(':'):
                        x = x.strip()
                        if not x:
                            continue
                        if x.endswith('.jar'):
                            hive_jars.append(x)
                        elif '*' in x:
                            xjars = glob.glob(x)
                            hive_jars += xjars

        # find hcat and webhcat jars
        #   https://issues.apache.org/jira/browse/HCATALOG-256
        hcat = self.cmddict.get('hcat', 'hcat')
        (hcat_dirs, hcat_jars) = Tracer.run_and_parse_classpath(cmd="%s -classpath" % hcat)

        webhcat_jars = self.findAllWebHcatJars()
        combined_jars = sorted(set(hcat_jars + webhcat_jars + hive_jars))
        self.classpath = ':'.join(combined_jars)
        self.aclasspath = ':'.join(combined_jars)

        # Remove excluded packages (DL+derby workaround)
        if self.options.excludepackage and not self.options.noexclusions:
            self.aclasspath = exclude_packages(self.classpath, self.options.excludepackage)
            self.classpath = self.aclasspath

        if self.jre and self.jdk and self.jar:
            if self.compileJar():
                LOG.info("hcatapi - compiling test jar successful")
                if self.runJar():
                    LOG.info("hcatapi - running test jar successful")
                    self.rc_strace = 0
                    self.rc_verbose = 0

                else:
                    LOG.error("hcatapi - running test jar failed")
                    return False
            else:
                LOG.error("hcatapi - compiling test jar failed")
                return False
        else:
            LOG.error("hcatapi - jre/jdk/jar not found")
            return False

        # Read test log and parse out the jars
        self.parsejars()
        self.jarfiles = self.jars
        LOG.info("hcatapi - total jars: %s" % len(self.jars))
        LOG.info("hcatapi - finished")

    ############################
    #   Helpers
    ############################

    def locateJREandJDK(self):

        jre = None
        jdk = None
        jar = None

        # If hive doesn't know, then use the process table
        if self.hiveinfo:
            if not 'system' in self.hiveinfo:
                x = [locatejdkbasedir()]
            elif not 'sun.boot.class.path' in self.hiveinfo['system']:
                x = [locatejdkbasedir()]
            else:
                if 'system' in self.hiveinfo:
                    if 'sun.boot.class.path' in self.hiveinfo['system']:
                        x = self.hiveinfo['system']['sun.boot.class.path'].split(':')
        else:
            x = [locatejdkbasedir()]

        x = sorted(set([os.path.dirname(y) for y in x if y]))

        for y in x:
            yparts = y.split("/")
            ylen = len(yparts)
            indexes = range(0, ylen + 1)
            indexes = reversed(indexes)
            for i in indexes:
                thisdir = "/".join(yparts[:i])

                if not jre:
                    cmd = "find %s -type f -name 'java'" % thisdir
                    (rc, so, se) = run_command(cmd)
                    if rc == 0:
                        jre = so.strip().split('\n')[0]
                    #import pdb; pdb.set_trace()

                if not jdk:
                    cmd = "find %s -type f -name 'javac'" % thisdir
                    (rc, so, se) = run_command(cmd)
                    if rc == 0:
                        jdk = so.strip()

                if not jar:
                    cmd = "find %s -type f -name 'jar'" % thisdir
                    (rc, so, se) = run_command(cmd)
                    if rc == 0:
                        jar = so.strip()

                if jre and jdk and jar:
                    break

        LOG.info("hcatapi - jres: %s" % jre)
        LOG.info("hcatapi - jdks: %s" % jdk)
        LOG.info("hcatapi - jars: %s" % jar)
        return jre, jdk, jar


    def findAllWebHcatJars(self, workdir=WORKDIR):

        """ Current revisions of hcatalog are a subproject of hive and the relevant
            jars can be found in $hivehome/hcatalog """

        # The hive-webhcat-java-client jar is an elusive file. It's not usually
        # part of any known service or command line's classpath, so we have to
        # do a lot of "fuzzing" on existing classpaths to find it.

        # Start by interrogating the hive classpath for directories/jars with
        # hive or hbase in the path or filename. Have to be careful here because
        # including too many jars in the final classpath will result in a java
        # exception for "too many open files". That is why this filters just the
        # hive|hcatalog jars and -HOPES- to get enough to compile the java client code
        if not hasattr(self, 'hiveinfo'):
            self.hiveinfo = collecthiveinfo(workdir=workdir)
        basecp = self.hiveinfo.get('env', {}).get('CLASSPATH', {})
        paths = []
        if type(basecp) == list:
            paths = [x.strip() for x in basecp if x.strip()]
        elif type(basecp) == str:
            paths = [x.strip() for x in basecp.split(':') if x.strip()]
        elif type(basecpd) == dict:
            pass
        paths = [x for x in paths if 'hive' in x or 'hbase' in x]
        cpr = javaClasspathReducer(':'.join(paths))

        globdirs = [os.path.dirname(x) for x in cpr.shortenedclasspath if x.endswith('*')]
        singlejars = [x for x in cpr.shortenedclasspath if x.endswith('.jar')]

        # now replace "lib" with "hcatalog" and check if it exists to find the hcatalog home(s)
        hcatalogdirs = []
        for dirpath in globdirs:

            thisparent = os.path.dirname(dirpath)
            thisgrandparent = os.path.dirname(thisparent)

            # workaround for newer cdh 5.3.2.x layouts
            if thisparent not in hcatalogdirs and thisparent:
                hcatalogdirs.append(thisparent)

            candidates = []
            candidates.append(os.path.join(thisparent, "hcatalog"))
            candidates.append(os.path.join(thisparent, "hive-hcatalog"))
            candidates.append(os.path.join(thisgrandparent, "hcatalog"))
            candidates.append(os.path.join(thisgrandparent, "hive-hcatalog"))

            for cp in candidates:
                if os.path.isdir(cp) and not cp in hcatalogdirs:
                    hcatalogdirs.append(cp)


        # now find all the jars in the hcatalog paths
        jars = []
        parcel_dirs = []
        for dirpath in hcatalogdirs:

            # If we find hive-hcatalog/share/webhcat/java-client, grab that
            # and exit early to avoid the other paths that might just cause
            # duplicate and conflicting jar versions.
            check_path = os.path.join(dirpath, 'share', 'webhcat', 'java-client')
            if os.path.exists(check_path):
                xjars = glob.glob(check_path + '/*.jar')
                if len(xjars) > 0:
                    LOG.info('hcatapi - found java-client dir at %s' % check_path)
                    return xjars

            cmd = "find %s -type f -name \"*.jar\"" % dirpath
            (rc, so, se) = run_command(cmd, checkrc=False, cwd=None)
            for line in so.split('\n'):
                line = line.strip()
                if line.endswith('.jar'):
                    if line not in jars:
                        jars.append(line)

            # Find the CDH parcel dir
            if 'cloudera/parcels' in dirpath:
                parts = dirpath.split('/')
                pindex = None
                for idx,x in enumerate(parts):
                    if x == 'parcels':
                        pindex = idx
                        break
                if pindex:
                    parcel_path = "/".join(parts[0:(pindex+2)])
                    if parcel_path not in parcel_dirs:
                        parcel_dirs.append(parcel_path)
                        LOG.info("hcatapi - CDH parcel path: %s" % parcel_path)

        # CDH doesn't like to put ALL of their jars in the right place,
        # so we have to locate a path to the parcels for this install
        # and then look for a "jars" directory where hopefully the
        # webhcat client can be found
        if parcel_dirs:
            # Use the longest path
            longest = None
            for x in parcel_dirs:
                if not longest:
                    longest = x
                elif len(x) > len(longest):
                    longest = x
            # Add jars in this path to the CP
            jpath = os.path.join(longest, "jars")
            if os.path.isdir(jpath):
                parcel_jars = glob.glob("%s/*.jar" % jpath)
                for pjar in parcel_jars:
                    if pjar not in jars:
                        jars.append(pjar)
                #import pdb; pdb.set_trace()

        # Check the process table for the hcatapi service and locate it's client jar dir
        # This only works if the webhcat services is running on the current machine.
        ps_client_cps = self.findWebHcatProcessCP()
        if ps_client_cps:
            for pscp in ps_client_cps:
                tjars = glob.glob(pscp)
                for tjar in tjars:
                    jars.append(os.path.abspath(tjar))

        # recombine with the single jars
        jars += singlejars
        jars = sorted(set(jars))
        return jars


    def findWebHcatProcessCP(self):
        # [etlguest@bdedev147 jamtan]$ ps aux | fgrep -i webhcat
        # hive     27473  0.0  1.1 836980 185256 ?       Sl   Jun24  15:15 /usr/../java -Xmx1000m
        # -Dwebhcat.log.dir=/var/log/hcatalog -Dlog4j.configuration=file:/.../webhcat-log4j.properties
        # -Dhadoop.log.dir=/opt/cloudera/parcels/CDH-5.2.0-1.cdh5.2.0.p0.36/lib/hadoop/logs
        # -Dhadoop.log.file=hadoop.log
        # -Dhadoop.home.dir=/opt/cloudera/parcels/CDH-5.2.0-1.cdh5.2.0.p0.36/lib/hadoop
        # -Dhadoop.id.str=
        # -Dhadoop.root.logger=INFO,console
        # -Djava.library.path=/opt/cloudera/parcels/CDH-5.2.0-1.cdh5.2.0.p0.36/lib/hadoop/lib/native
        # -Dhadoop.policy.file=hadoop-policy.xml
        # -Djava.net.preferIPv4Stack=true
        # -Djava.net.preferIPv4Stack=true
        # -Djava.net.preferIPv4Stack=true
        # -Xms268435456 -Xmx268435456 -XX:+UseParNewGC -XX:+UseConcMarkSweepGC
        # -XX:-CMSConcurrentMTEnabled -XX:CMSInitiatingOccupancyFraction=70
        # -XX:+CMSParallelRemarkEnabled -XX:OnOutOfMemoryError=/usr/lib64/cmf/service/common/killparent.sh
        # -Dhadoop.security.logger=INFO,NullAppender org.apache.hadoop.util.RunJar
        # /opt/.../hive-webhcat-0.13.1-cdh5.2.0.jar org.apache.hive.hcatalog.templeton.Main

        jarpaths = []
        cmd = "ps aux | fgrep -i webhcat"
        (rc, so, se) = run_command(cmd, checkrc=False, cwd=None)
        if rc != 0:
            return None

        lines = [x for x in so.split('\n') if 'java' in x]
        for line in lines:
            parts = line.split()
            thisjar = None

            runjar_idx = None
            for idx,x in enumerate(parts):
                if x == 'org.apache.hadoop.util.RunJar':
                    runjar_idx = idx
                    break
            if runjar_idx:
                thisjar = parts[runjar_idx + 1]
                LOG.info("hcatapi - service jar located at %s" % thisjar)
                if 'webhcat' in thisjar:
                    jarpaths.append(os.path.abspath(thisjar))
                else:
                    break


        # seek higher dirs for the client jars
        clientdirs = []
        for jarpath in jarpaths:

            thisdir = os.path.dirname(jarpath)
            thisparent = os.path.dirname(thisdir)
            thisgrandparent = os.path.dirname(thisparent)

            candidates = []
            candidates.append(os.path.join(thisparent, "java-client"))
            candidates.append(os.path.join(thisgrandparent, "java-client"))

            for cp in candidates:
                if os.path.isdir(cp):
                    LOG.info("hcatapi - client dir at %s" % cp)
                    clientdirs.append(os.path.join(cp, '*'))

        #import pdb; pdb.set_trace()
        return clientdirs

    def jarsToClassPath(self, jars):
        dirnames = []
        for x in jars:
            dirname = os.path.dirname(x)
            if dirname not in dirnames:
                dirnames.append(dirname)
        dirnames = "/*:".join(dirnames) + '/*'
        return dirnames

    def compileJar(self):
        LOG.info("hcatapi - compiling test jar")

        s = Template(HCAPI)
        tdata = s.substitute(thrift_uri=self.thrifturi)

        fname = os.path.join(self.workdir, "TestHCatClient.java")
        f = open(fname, "wb")
        f.write(tdata)
        f.close()

        # Chop up the classpath into multiple lines to avoid max command lengths
        BASHCP = ""
        CPS = [x for x in self.aclasspath.split(':') if x]
        for idx,x in enumerate(CPS):
            if idx == 0:
                BASHCP = 'export CLASSPATH="%s"\n' % x
            else:
                BASHCP += 'export CLASSPATH="$CLASSPATH:%s"\n' % x

        bscript = "#!/bin/bash\n"
        bscript += BASHCP
        bscript += "rm -rf htest_classes\n"
        bscript += "mkdir htest_classes\n"
        bscript += "%s -Xlint:deprecation -d htest_classes -g" % self.jdk
        bscript += " TestHCatClient.java\n"
        bscript += "RC=$?\n"
        bscript += "if [ $RC != 0 ];then\n"
        bscript += "    exit 1\n"
        bscript += "fi\n"
        bscript += "RC=$?\n"
        bscript += "rm -f hts-hcat.jar\n"
        bscript += "%s -cvf hts-hcat.jar -C htest_classes .\n" % self.jar
        bscript += "if [ $RC != 0 ];then\n"
        bscript += "    exit 1\n"
        bscript += "fi\n"

        bname = os.path.join(self.workdir, "make.sh")
        f = open(bname, "wb")
        f.write(bscript)
        f.close()
        st = os.stat(bname)
        os.chmod(bname, st.st_mode | stat.S_IEXEC)

        cmd = "./make.sh"
        (rc, so, se) = run_command(cmd, cwd=self.workdir)

        jarf = os.path.join(self.workdir, "hts-hcat.jar")

        if os.path.isfile(jarf) and rc == 0:
            return True
        else:
            data = str(so) + str(se)
            data = data.split('\n')
            for line in data:
                if 'error: ' in line:
                    LOG.error("hcatapi [compilejar] - %s" % line)

            return False

    def runJar(self):
        LOG.info("hcatapi - running test jar")

        # Chop up the classpath into multiple lines to avoid max command lengths
        BASHCP = ""
        CPS = [x for x in self.aclasspath.split(':') if x]
        for idx,x in enumerate(CPS):
            if idx == 0:
                BASHCP = 'export CLASSPATH="%s"\n' % x
            else:
                BASHCP += 'export CLASSPATH="$CLASSPATH:%s"\n' % x
        BASHCP += 'export CLASSPATH="$CLASSPATH:$(pwd)/hts-hcat.jar"\n'

        testscr = "#!/bin/bash\n"
        testscr += BASHCP
        testscr += "%s -verbose:class org.hts.hcat.TestHCatClient" % self.jre
        testscr += " > test.log 2>&1\n"
        testscr += "RC=$?\n"
        testscr += "if [ $RC != 0 ];then\n"
        testscr += "    exit 1\n"
        testscr += "fi\n"

        fname = os.path.join(self.workdir, "test.sh")
        f = open(fname, "wb")
        f.write(testscr)
        f.close()
        st = os.stat(fname)
        os.chmod(fname, st.st_mode | stat.S_IEXEC)

        cmd = "./test.sh"
        (rc, so, se) = run_command(cmd, cwd=self.workdir)

        tlog = os.path.join(self.workdir, "test.log")

        if os.path.isfile(tlog):
            f = open(tlog, "rb")
            data = f.readlines()
            f.close()

        # Show what failed
        if rc != 0:
            if rc == 9:
                LOG.error("hcatapi - exceeded timeout")
            for line in [x for x in data if x]:
                if "error" in line.lower() or "exception" in line.lower() or "refused" in line.lower():
                    if not "loaded " in line.lower():
                        LOG.error("hcatapi - %s" % line.strip())

        #import pdb; pdb.set_trace()
        if (os.path.isfile(tlog) and rc == 0) or (not self.options.stoponerror):
            return True
        else:
            return False

    def parsejars(self):
        fname = os.path.join(self.workdir, "test.log")
        f = open(fname, "rb")
        data = f.read()
        f.close()
        self.classpaths = parseverboseoutput(data)
        self.jars = classpathstojars(self.classpaths)
        self.jars = [x for x in self.jars if 'hts-hcat.jar' not in x]
        self.jars = Tracer.jrejarfilter(self.jre, self.jars)


############################################################
#   THRIFT HELPER CODE
############################################################

THRIFTCODE = """
import org.apache.thrift.TException;

public class ThriftExceptionFinder {

  /**
   * @param args
   * @throws TException
   */
  public static void main(String[] args) throws TException {

    System.out.println("Starting thrift code  ...");

  }
}
"""

class ThriftTrace(Tracer):

    # Primary target(s):
    #   * libthrift-0.9.0-cdh5-2.jar
    #       - import org.apache.thrift.TException;
    #[Loaded org.apache.thrift.TException from file:/opt/mapr/hive/hive-0.13/lib/hive-exec-0.13.0-mapr-1501.jar]
    #/mapr/demo.mapr.com/oozie/share/lib/hive/libthrift-0.9.0.jar,org/apache/thrift/TException.class
    #/opt/mapr/oozie/oozie-4.1.0/share1/lib/hive/libthrift-0.9.0.jar,org/apache/thrift/TException.class
    #/opt/mapr/oozie/oozie-4.1.0/share2/lib/hive/libthrift-0.9.0.jar,org/apache/thrift/TException.class
    #/opt/mapr/hive/hive-0.13/lib/libthrift-0.9.0.jar,org/apache/thrift/TException.class
    #/opt/mapr/hbase/hbase-0.98.9/lib/libthrift-0.9.0.jar,org/apache/thrift/TException.class

    def Run(self):

        if not os.path.isdir(WORKDIR):
            os.makedirs(WORKDIR)
        self.workdir = tempfile.mkdtemp(prefix='thrift.', dir=WORKDIR)

        # What is the jre/jdk path?
        # system:sun.boot.class.path=/usr/java/jdk1.7.0_67-cloudera/...:...:
        (self.jdk, self.jre, self.jar) = Tracer.get_jdk_jre_jar_commands()

        LOG.info("thrift - fetching hive classpath")
        (hive_dirs, hive_jars) = Tracer.gethiveclasspath(preferhivelib=False)
        hive_classpath = hive_dirs + hive_jars
        LOG.info("thrift - hiveclasspath: %s" % len(hive_classpath))

        # Chop up the classpath into multiple lines to avoid max command lengths
        BASHCP = ""
        for idx,x in enumerate(hive_classpath):
            if idx == 0:
                BASHCP = 'export CLASSPATH="%s"\n' % x
            else:
                BASHCP += 'export CLASSPATH="$CLASSPATH:%s"\n' % x
        BASHCP += 'export CLASSPATH="$CLASSPATH:$(pwd)/."\n'

        # Templatize and replace the build script ...
        ddict = {'CODE': THRIFTCODE,
                 'CLASSPATH': BASHCP,
                 'JDK': self.jdk,
                 'JRE': self.jre}
        s = Template(HIVEBUILDSCRIPT)
        bs = s.substitute(ddict)
        bs = bs.replace('HiveJdbcClient', 'ThriftExceptionFinder')
        bs = bs.replace('hivejava.debug', 'thriftjava.debug')

        # write out buildscript
        makefile = os.path.join(self.workdir, "makefile")
        LOG.info("thrift - makefile: %s" % makefile)
        fh = open(makefile, "wb")
        fh.write(bs)
        fh.close()

        # run buildscript
        args = "/bin/bash %s" % makefile
        (rc, so, se) = run_command(args, cwd=self.workdir)
        self.rc_verbose = rc
        self.rc_strace = rc

        if rc != 0:
            LOG.error("thrift - %s" % so + se)

        # The classloader output is in the *.debug file
        debugfile = os.path.join(self.workdir, "thriftjava.debug")
        if not os.path.isfile(debugfile):
            LOG.error("thrift - %s missing" % debugfile)
            return None, None
        else:
            LOG.info("thrift - %s" % debugfile)

        LOG.info('thrift - parsing verbose log')

        f = open(debugfile, "rb")
        data = f.read()
        f.close()

        self.classpaths = []
        classpaths = parseverboseoutput(data)
        texceptionjar = None
        if classpaths:
            for cp in classpaths:
                if cp[0] == 'org.apache.thrift.TException':
                    texceptionjar = cp[1]
                if cp not in self.classpaths:
                    self.classpaths.append(cp)

            self.jars = classpathstojars(classpaths)
            if self.jars:
                self.jars = [x for x in Tracer.jrejarfilter(self.jre, self.jars)]

        if texceptionjar:
            LOG.info("thrift - org.apache.thrift.TException found in %s" % texceptionjar)
        else:
            LOG.error("thrift - org.apache.thrift.TException not found")

        self.jarfiles = self.jars
        LOG.info("thrift - %s total jars" % len(self.jarfiles))
        LOG.info("thrift - tracer finished")


    @staticmethod
    def checkthriftport():
        return True



############################################################
#   OOZIE
############################################################

class OozieTrace(Tracer):

    """ Find the oozie metadata from the process table """

    def Run(self):
        self.findOozieProcess()
        self.findOozieSiteXML()

        # [ignored - oozie.rc.cmd_strace oozie.rc.java_verbose ]
        self.rc_strace = 0
        self.rc_verbose = 0


    def findOozieProcess(self):
        cmd = "ps aux"
        (rc, so, se) = run_command(cmd)
        lines = [x for x in so.split('\n') if x]
        lines = [x for x in lines if 'oozie.config.dir' in x]
        if len(lines) >= 1:
            parts = shlex.split(lines[0])
            for part in parts:
                if part.startswith('-D') and '=' in part:
                    part = part.replace('-D', '', 1)
                    plist = part.split('=', 1)
                    k = plist[0]
                    v = plist[1]
                    self.metadata[k] = v


    def findOozieSiteXML(self):
        configfile = None
        configdir = None
        configfilepath = None
        if 'oozie.config.file' in self.metadata:
            configfile = self.metadata['oozie.config.file']
        if 'oozie.config.dir' in self.metadata:
            configdir = self.metadata['oozie.config.dir']

        if configdir and configfile:
            configfilepath = os.path.join(configdir, configfile)
            if os.path.isfile(configfilepath):
                self.sitexmls=[configfilepath]

############################################################
#   BEELINE
############################################################

class BeelineJdbcTrace(Tracer):

    """ Get the jars for beeline """

    def Run(self):
        self.beeline = getcmdpath('beeline')

        if self.options.hivejdbcurl:
            self.metadata['connection_params'] = self.options.hivejdbcurl.split()
        else:
            self.hiveinfo = collecthiveinfo(workdir=WORKDIR, options=self.options)
            self.hivetracer = HiveJdbcTrace()
            self.hivetracer.options = self.options
            self.hivetracer.hiveinfo = self.hiveinfo
            self.hivetracer.set_principal()
            self.hivetracer.set_jdbc_params()
            LOG.info("beeline - jdbcparams: %s" % self.hivetracer.jdbcparams)
            self.metadata['connection_params'] = self.hivetracer.metadata['connection_params']

        # Use the hive hostname the user specified
        if self.options.hivehost and not self.options.hivejdbcurl:
            if self.options.hivehost not in self.metadata['connection_params'][0]:
                # split the host and port ...
                slash_parts = self.metadata['connection_params'][0].split('/')
                hostport = slash_parts[2].split(':')
                hostport[0] = self.options.hivehost

                # rejoin and save
                slash_parts[2] = ':'.join(hostport)
                self.metadata['connection_params'][0] = '/'.join(slash_parts)

        if not self.beeline:

            # Is beeline next to hive? (mapr)
            hive = getcmdpath('hive')
            if hive:
                if os.path.islink(hive):
                    hive = os.path.realpath(hive)

                bindir = os.path.dirname(hive)
                beelinecmd = os.path.join(bindir, "beeline")
                if os.path.isfile(beelinecmd):
                    LOG.info('beeline found at %s' % beelinecmd)
                    self.beeline = beelinecmd

            if not self.beeline:
                LOG.error("beeline is not in this users path")
                return False

        # write out the sql cmds
        sqlfile = os.path.join(WORKDIR, "beeline-query.sql")
        f = open(sqlfile, "wb")
        f.write("show tables;\n")
        f.close()

        #beeline -u jdbc:hive2://localhost:10000/default -e "show tables"
        if len(self.metadata['connection_params']) == 1:
            cmd = "%s --color=false -u \"%s\" -f %s" % (self.beeline,
                                                    self.metadata['connection_params'][0],
                                                    sqlfile)
        else:
            # Beeline doesn't like empty password strings ...
            if not self.metadata['connection_params'][2]:
                self.metadata['connection_params'][2] = "NULL"

            cmd = "%s --color=false -u \"%s\" -n %s -p \"%s\" -f %s" % (self.beeline,
                                                    self.metadata['connection_params'][0],
                                                    self.metadata['connection_params'][1],
                                                    self.metadata['connection_params'][2],
                                                    sqlfile)

        # Beeline uses the HADOOP_CLASSPATH, so we need to tell strace
        # to include it or the hivedriver may not be found after jar exclusions.
        LOG.info("beeline - cmd to strace: %s" % cmd)
        self.strace(cmd, svckey="beeline", piping=False, shorten=False,
                    use_hcp=False, usetimeout=True, timeout=TIMEOUT)
        if ( self.rc_strace == 0 and self.rc_verbose != 0 ):
            # Re-run with a shortened classpath
            self.strace(cmd, svckey="beeline", piping=False, shorten=True,
                        use_hcp=True, usetimeout=True, timeout=TIMEOUT)
        LOG.info("beeline - finished")


############################################################
#   PIG
############################################################

class PigTrace(Tracer):

    """ Get the jars for pig """

    CODE = """rmf /tmp/pigtracer.$username/output
A = LOAD '/tmp/pigtracer.$username/indata' USING PigStorage(',')
    AS (customer_number,account_number,status);
B = FILTER A BY status == 'foo';
store B into '/tmp/pigtracer.$username/output' USING PigStorage(',');
"""

    DATA = """customer_number,account_number,status
1,1,foo
2,2,foo
3,3,bar
4,4,foo
5,5,baz
6,6,foo
"""

    '''
    http://sww.sas.com/cgi-bin/quick_browse?defectid=S1183548
    -bash-4.1$ hostname -f
    qstconlax14.unx.sas.com
    -bash-4.1$ pig -printCmdDebug | egrep ^HADOOP_CLASSPATH | awk '{print $2}' | tr ':' '\n' | fgrep -i auto
    /usr/iop/4.1.0.0/pig/lib/automaton-1.11-8.jar
    '''

    def Run(self):

        # NOTE: pig's classpath can be displayed with -printCmdDebug
        '''
        [mapr@maprdemo pig.wF7V4p]$ pig -printCmdDebug
        Find hadoop at /usr/bin/hadoop
        dry run:
        HADOOP_CLASSPATH: :/opt/mapr/pig/pig-0.12/bin/../conf:/ ...
        HADOOP_OPTS:
            -Xmx1000m
            -Dpig.log.dir=/opt/mapr/pig/pig-0.12/bin/../logs
            -Dpig.log.file=pig.log -Dpig.home.dir=/opt/mapr/pig/pig-0.12/bin/..
            -Dhadoop.login=simple
            -Djava.security.auth.login.config=/opt/mapr/conf/mapr.login.conf
            -Dzookeeper.sasl.clientconfig=Client_simple
            -Dzookeeper.saslprovider=com.mapr.security.simplesasl.SimpleSaslProvider
            -Dmapr.library.flatclass
        /usr/bin/hadoop jar /opt/mapr/pig/pig-0.12/bin/../pig-withouthadoop.jar
        '''

        self.workdir = tempfile.mkdtemp(prefix='pig.', dir=WORKDIR)
        self.pig = getcmdpath('pig')
        info = get_local_environment()

        # Create the dataset csv in the workdir
        fname = os.path.join(self.workdir, "test.csv")
        f = open(fname, "wb")
        f.write(self.DATA)
        f.close()

        # Clean and create other hdfs tmpdir
        cmd = "hadoop fs -rm -r -f -skipTrash /tmp/pigtracer.%s" % info['username']
        (rc, so, se) = run_command(cmd)
        cmd = "hadoop fs -mkdir -p /tmp/pigtracer.%s/indata" % info['username']
        (rc, so, se) = run_command(cmd)

        # Copy the dataset to hdfs
        cmd = "hadoop fs -copyFromLocal %s /tmp/pigtracer.%s/indata/test.csv" % (fname, info['username'])
        (rc, so, se) = run_command(cmd)

        # Write out the example code
        s = Template(self.CODE)
        code = s.substitute(info)
        fname = os.path.join(self.workdir, "test.pig")
        f = open(fname, "wb")
        f.write(code)
        f.close()

        # Strace the pig command
        cmd = "%s -x mapreduce -f %s" % (self.pig, fname)
        self.strace(cmd, svckey='pig', piping=True)

        # Cleanup
        cmd = "hadoop fs -rm -r -f -skipTrash /tmp/pigtracer.%s" % info['username']
        (rc, so, se) = run_command(cmd)


############################################################
#   CLASSPATH REDUCER
############################################################

class javaClasspathReducer(object):

    def __init__(self, classpath):

        self.classpath = classpath

        # make a unique list of cp's
        self.classpaths = self.classpathunique(self.classpath)

        # flatten the list to real files
        self.classdirs, self.flatfiles = self.flattenclasspathtofiles(self.classpaths)

        # reduce the flattened list
        self.reducedpaths = self.filepathreducer(self.flatfiles[:])

        # retain the dirs and combine with paths
        self.shortenedclasspath = sorted(set(self.classdirs + self.reducedpaths))

        # Make a flat list of jars for optional use
        self.jarfiles = self.flattenjars()


    def __exit__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self


    def flattenclasspathtofiles(self, classpaths):

            """Flatten a java classpath to a list of
               absolute file paths based on java's
               classloader behavior """

            dirs = []
            files = []

            # break out multi-line filenames: S1152653
            for idx,x in enumerate(classpaths):
                if '\n' in x:
                    parts = x.split('\n')
                    classpaths += parts
                    del classpaths[idx]

            for cp in classpaths:

                # directory
                if cp.endswith('/'):
                    #print "directory ..."
                    # get jars AND classes
                    jarfiles = glob.glob("%s/*.jar" % cp)
                    classfiles = glob.glob("%s/*.class" % cp)
                    testfiles = jarfiles + classfiles
                    for tf in testfiles:
                        if os.path.isfile(tf):
                            tf = os.path.abspath(tf)
                            files.append(tf)

                # single jar
                elif cp.endswith('.jar'):
                    #print "jar ..."
                    cp = os.path.abspath(cp)
                    files.append(cp)

                # glob
                elif cp.endswith('/*'):

                    cp = "%s.jar" % cp
                    #print "glob ...", cp

                    dirglob = glob.glob(cp)
                    for dirfile in dirglob:
                        if os.path.isfile(dirfile):
                            # make sure it's an absolute path
                            dirfile = os.path.abspath(dirfile)
                            files.append(dirfile)
                        elif os.path.islink(dirfile):
                            sl = os.path.abspath(dirfile)
                            if os.path.isfile(sl):
                                files.append(sl)

                        else:
                            #print "What is this?",dirfile
                            pass

                # other (must discover)
                else:
                    if os.path.isdir(cp):
                        #print "pydir ..."

                        # keep track of this dir for confs
                        if os.path.abspath(cp) not in dirs:
                            dirs.append(os.path.abspath(cp))

                        jarfiles = glob.glob("%s/*.jar" % cp)
                        classfiles = glob.glob("%s/*.class" % cp)
                        testfiles = jarfiles + classfiles
                        for tf in testfiles:
                            if os.path.isfile(tf):
                                tf = os.path.abspath(tf)
                                files.append(tf)

                    elif os.path.isfile(cp):
                        #print "pyfile ..."
                        tf = os.path.abspath(cp)
                        files.append(tf)
                    else:
                        #print "unknown ..."
                        pass

            return dirs, files


    def classpathunique(self, classpath):

        """Split a string of classpaths and unique the list"""

        if not classpath:
            return classpath

        classpaths = classpath.split(':')
        classpaths = [x for x in classpaths if x]
        classpaths = sorted(set(classpaths))
        return classpaths


    def filepathreducer(self, files):

        """Given a list of files, shorten the list
           for each path to a glob if all files in the
           basedir are defined """

        # max # of unused jars before not globbing a dir
        threshold = 2

        # make a list of dirpaths
        dirs = []
        for f in files:
            dir = os.path.dirname(f)
            #print dir
            #if dir.endswith('.'):
            if dir not in dirs:
                dirs.append(dir)

        # get the list of files in the dir
        for dir in dirs:
            dirjars = glob.glob("%s/*.jar" % dir)
            dirjars += glob.glob("%s/*.class" % dir)
            undefined = []
            for dj in dirjars:
                if dj not in files:
                    #print "UNDEFINED:",dj
                    undefined.append(dj)
                    #alldefined = False

            if len(undefined) > threshold:
                #print undefined
                #print "### %s could not be consolidated" % dir
                pass
            else:
                #print "### %s can be consolidated" % dir
                for idx,x in enumerate(files):
                    if dir  == os.path.dirname(x):
                        files[idx] = "%s/*" % dir

        files = sorted(set(files))
        return files


    def flattenjars(self):

        """ Make a flat list of absolute jars from the shortened CP """

        self.jars = []
        #print self.shortenedclasspath
        for cp in self.shortenedclasspath:
            if cp.endswith('.jar'):
                if cp not in self.jars:
                    self.jars.append(cp)
                continue
            if cp.endswith('/*'):
                gjars = glob.glob(cp + '.jar')
                for gjar in gjars:
                    if gjar not in self.jars:
                        self.jars.append(gjar)
        #import pdb; pdb.set_trace()


############################################################
#   TRACER HELPER FUNCTIONS
############################################################

def list_jar_contents(jarfile):
    global JCCACHE

    if jarfile in JCCACHE:
        return JCCACHE[jarfile]

    jarcontent = []
    ecmd = None
    candidates = ['unzip', 'zip', 'jar']
    for can in candidates:
        if checkcmdinpath(can):
            ecmd = can
            break
    if ecmd == "unzip":
        cmd = "unzip -l %s" % jarfile
        (rc, so, se) = run_command(cmd, checkrc=False)
        lines = [x for x in so.split('\n') if x]
        for x in lines:
            parts = x.split()
            if not len(parts) == 4:
                continue
            jarcontent.append(parts[-1])

    elif ecmd == "jar":
        #[root@jt-cdh5-0 ~]# jar -tf /tmp/jars/derby-10.11.1.1.jar | head
        #META-INF/MANIFEST.MF
        #org/apache/derby/agg/Aggregator.class

        cmd = "jar -tf %s" % jarfile
        (rc, so, se) = run_command(cmd, checkrc=False)
        jarcontent = [x for x in so.split('\n') if x]


    # save to cache
    JCCACHE[jarfile] = jarcontent

    return jarcontent

def exclude_packages(classpath, excludepackages, shorten=False):
    # take a classpath, break it down to jars, inspect jars,
    # exclude any jars that have blacklisted packages

    # FIXME -- make sure the ordering is not altered
    # FIXME -- make sure the directories are saved

    jarmap = {}
    global JCEXCLUSIONS

    if not classpath or not excludepackages:
        return classpath

    jcpr = javaClasspathReducer(classpath)

    # figure out if any jars have exclusions in them
    for jar in jcpr.jars:
        if jar in JCEXCLUSIONS:
            continue
        jarmap[jar] = {}
        flagged = False
        files = list_jar_contents(jar)
        jarmap[jar]['files'] = files
        for file in files:
            for exp in excludepackages:
                if file.startswith(exp):
                    flagged = True
        jarmap[jar]['flagged'] = flagged
        #if flagged and os.path.basename(jar) != 'hive-jdbc.jar':
        if flagged:
            LOG.info("exclusion -- %s" % jar)
            JCEXCLUSIONS.append(jar)

    # make a new classpath without the exclusions
    if shorten:
        newcp = [x for x in jcpr.jars if x not in JCEXCLUSIONS]
        jcpr2 = javaClasspathReducer(':'.join(newcp))
        newclasspath = jcpr2.shortenedclasspath
    else:
        # preserve the original classpath ordering
        newcp = []
        for path in classpath.split(':'):
            # do away with the /../ style paths
            path = os.path.abspath(path)
            if path.endswith('.jar') and path not in JCEXCLUSIONS:
                newcp.append(path)
            elif os.path.isdir(path):
                newcp.append(path)
            elif path.endswith('/*'):
                # Need to get the list of jars and remove exclusions
                jars = glob.glob(path)
                jars2 = [x for x in jars if x not in JCEXCLUSIONS]
                if jars2 == jars:
                    newcp.append(path)
                else:
                    newcp = newcp + jars2
        newclasspath = newcp

    return ':'.join(newclasspath)


def locatejdkbasedir():
    # use the process table to find a valid JDK path
    jres = []
    jdks = []

    cmd = "ps aux | fgrep -i java"
    (rc, so, se) = run_command(cmd, checkrc=False)
    if rc != 0:
        return None

    # split apart the lines and find running jres
    lines = so.split('\n')
    for line in lines:
        parts = shlex.split(line)
        if len(parts) < 10:
            continue
        if not parts[10].endswith('bin/java'):
            continue
        if os.path.isfile(parts[10]) and not parts[10] in jres:
            jres.append(parts[10])

    # append a 'c' to the jre and see if it's a real file
    for jre in jres:
        basedir = os.path.dirname(jre)
        jdk = os.path.join(basedir, 'javac')
        jarcmd = os.path.join(basedir, 'jar')
        if os.path.isfile(jdk) and os.path.isfile(jarcmd):
            jdks.append(basedir)

    if len(jdks) == 0:
        return None
    elif len(jdks) == 1:
        return jdks[0]
    else:
        return jdks[0]


def hadoopclasspathcmd():

    """ Find all jars listed by the hadoop classpath command """

    LOG.info("hadoop-classpath - locating all jars")

    jars = []
    cmd = "hadoop classpath"
    rc, so, se = run_command(cmd, checkrc=False)

    if rc != 0:
        return jars

    # Split and iterate each path
    paths = so.split(':')
    for path in paths:
        files = glob.glob(path)
        for file in files:
            if file.endswith(".jar"):
                jars.append(file)

    return jars


def striplast(line, delimiter):

    """ Reverse a string, strip up to delimiter """

    backwards = line[::-1]
    parts = backwards.split(delimiter, 1)
    forwards = parts[1][::-1]
    #import pdb; pdb.set_trace()
    return forwards


def splitoutterarray(line):

    """ Get the outtermost array defined by [] in a string """

    result = None

    # strip to the first [
    parts1 = line.split('[', 1)

    # strip after the last ]
    strlist = striplast(parts1[1], ']')

    # cast to a real list
    try:
        result = ast.literal_eval('[' + strlist + ']')
    except Exception:
        # move on if not a good list
        result = None

    return result


def splitexecve(line):
    '''
    [pid 31338] 21:16:03 execve("/usr/java/latest/bin/java",
        ["/usr/java/latest/bin/java", "-Xmx256m", "-server",
            "-Dhadoop.log.dir=/home/hadoop/logs", "-Dhadoop.log.file=hadoop.log",
            "-Dhadoop.home.dir=/home/hadoop", "-Dhadoop.id.str=",
            "-Dhadoop.root.logger=INFO,console",
            "-Djava.library.path=/home/hadoop/lib/native",
            "-Dhadoop.policy.file=hadoop-policy.xml",
            "-Djava.net.preferIPv4Stack=true", "-XX:MaxPermSize=128m",
            "-Dhadoop.security.logger=INFO,NullAppender",
            "-Dsun.net.inetaddr.ttl=30", "org.apache.hadoop.util.VersionInfo" ],
        [ ENVIRONMENT ]
    '''

    if 'execve' not in line:
        return None, None

    # drop everything before the command
    parts1 = line.split('(', 1)

    # get everything after the first [
    parts2 = parts1[1].split('[', 1)

    # get everything before the first ]
    parts3 = parts2[1].split(']', 1)

    ## ARGLIST
    # try to convert the string to a list
    arglist = '[' + parts3[0] + ']'
    try:
        arglist = ast.literal_eval(arglist)
    except Exception:
        arglist = None

    ## ENVIRONMENT
    envlist = splitoutterarray(parts3[1])

    #return JAVACMD, JAVAENV
    return arglist, envlist


def getcmdpath(cmd):

    """ Get the path for a command """

    if len(shlex.split(cmd)) > 1:
        cmd = shlex.split(cmd)[0]
    args = "which %s" % cmd

    p = Popen(args, stdout=PIPE, stderr=PIPE, shell=True)
    so, se = p.communicate()

    return so.strip()


def checkcmdinpath(cmd):

    """ Verify a command is in the user's path """

    if len(shlex.split(cmd)) > 1:
        cmd = shlex.split(cmd)[0]
    args = "which %s" % cmd

    p = Popen(args, stdout=PIPE, stderr=PIPE, shell=True)
    so, se = p.communicate()

    if p.returncode == 0:
        return True
    else:
        return False


def bashtimeout(workdir=WORKDIR, timeout=TIMEOUT):

    # SLES 11sp1 does not provide the timeout command
    # with it's coreutils package. This bash script can
    # simulate the timeout command's functionality

    # http://stackoverflow.com/a/11056286
    code = '''
    #!/bin/bash
    TIMEOUT=%s
    ( $@ ) & pid=$!
    ( sleep $TIMEOUT && kill -HUP $pid ) 2>/dev/null & watcher=$!
    wait $pid 2>/dev/null && pkill -HUP -P $watcher
    ''' % timeout.replace('s', '')

    codelines = [x.lstrip() for x in code.split('\n') if x]

    # create the file if not already created
    fname = os.path.join(workdir, 'timeout')
    if not os.path.isfile(fname):
        f = open(fname, "wb")
        for line in codelines:
            f.write(line + '\n')
        f.close()

    st = os.stat(fname)
    os.chmod(fname, st.st_mode | stat.S_IEXEC)

    return fname


def run_command(cmd, checkrc=False, cwd=None, timeout=TIMEOUT):

    """ Run a shell command """

    timeoutcmd = None
    if checkcmdinpath('timeout'):
        timeoutcmd = getcmdpath('timeout')
        cmd = "%s -s SIGKILL %s %s" % (timeoutcmd, timeout, cmd)
    else:
        btimeoutcmd = bashtimeout()
        cmd = "%s %s" % (btimeoutcmd, cmd)

    p = Popen(cmd, cwd=cwd, stdout=PIPE, stderr=PIPE, shell=True)
    so, se = p.communicate()
    rc = p.returncode
    if rc != 0 and checkrc:
        LOG.error("cmd: %s\n#\trc: %s" % (cmd, rc))
        LOG.error("cmd: %s\tso|se: %s" % (cmd, str(so) + str(se)))
        #sys.exit(1)
    return rc, so, se

def run_command_live(args, cwd=None, shell=True, checkrc=False):
    p = subprocess.Popen(args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            shell=shell)
    so = ""
    while p.poll() is None:
        lo = p.stdout.readline() # This blocks until it receives a newline.
        sys.stdout.write(lo)
        so += lo
    print p.stdout.read()

    return (p.returncode, so, "")


def get_hadoop_classpath(rawtext):

    ''' Find the last HADOOP_CLASSPATH reference in strace '''

    HADOOP_CLASSPATH = None
    lines = rawtext.split('\n')
    lines = [x for x in lines if 'HADOOP_CLASSPATH' in x]
    lines = reversed(lines)
    for x in lines:
        parts = x.split(',')
        for idy,y in enumerate(parts):
            if 'HADOOP_CLASSPATH=' in y:

                y = y.replace('HADOOP_CLASSPATH=', '', 1)

                # Fix mapr's broken classpaths
                cps = [z.strip() for z in y.split(':') if z and z != '"']
                for idz, z in enumerate(cps):
                    if '\\n' in z:
                        zparts = z.split('\\n')
                        cps[idz] = zparts[0]
                        for zp in reversed(zparts[1:]):
                            cps.insert(idz+1, zp)

                HADOOP_CLASSPATH = ':'.join(cps)
                break
        if HADOOP_CLASSPATH:
            break

    return HADOOP_CLASSPATH


def parse_strace_output(rawtext, shorten=False):

    """ Pull java related information from raw strace output """

    CLASSPATH = None
    JRE = None
    JAVACMD = None
    JAVAENV = None

    # look for execve
    for x in (rawtext).split("\n"):
        if 'bin/java' in x \
            and 'execve(' in x \
            and x.strip().endswith('= 0') \
            and not '<unfinished ...>' in x:

            # pick apart this line into a java command and an env
            tmpcmd, tmpenv = splitexecve(x)
            if tmpcmd is not None and tmpenv is not None:
                # skip weird non-java execves
                if not tmpcmd[0].endswith('java'):
                    continue
                else:
                    JAVACMD = tmpcmd
                    JAVAENV = tmpenv

                # workaround to re-quote -e strings for hive/beeline
                if JAVACMD[-2] == "-e":
                    JAVACMD[-1] = '"' + JAVACMD[-1] + '"'

    if type(JAVACMD) != list:
        return None, None, None, None

    CPS = [x for x in JAVAENV if x.startswith('CLASSPATH=')]
    if CPS:
        CLASSPATH = CPS[0]
        CLASSPATH = CLASSPATH.replace('CLASSPATH=', '')

    if not CLASSPATH:
        # did any positional args have a classpath?
        cp_idx = None
        for idx, val in enumerate(JAVACMD):
            if val == '-classpath' or val == '-cp':
                cp_idx = idx + 1
        if cp_idx:
            CLASSPATH = JAVACMD[cp_idx]

    # clean up the classpath
    if shorten:
        with javaClasspathReducer(CLASSPATH) as cpr:
            #cpr = javaClasspathReducer(CLASSPATH)
            if cpr.shortenedclasspath:
                CLASSPATH = ':'.join(cpr.shortenedclasspath)

    if JAVACMD[0].endswith('/java') or JAVACMD[0] == "java":
        JRE = JAVACMD[0]

    #types STR  STRING     LIST     LIST
    return JRE, CLASSPATH, JAVACMD, JAVAENV


def parse_strace_open_file(rawtext, filename, list=False):

    """ Return the last path a filename was opened from """

    #17:02:29 open("/etc/issues", O_RDONLY)  = -1 ENOENT (No such file or directory)
    #17:02:36 open("/etc/issue", O_RDONLY)   = 3
    results = []
    for x in rawtext.split("\n"):
        if not ' open(' in x:
            continue
        if "ENOENT" in x:
            continue
        parts = [y for y in shlex.split(x)]
        if len(parts) == 0:
            continue
        if parts[-2] != '=':
            continue
        open_idx = None
        for idx, part in enumerate(parts):
            if part.startswith('open'):
                open_idx = idx
                break
        if open_idx:
            # open("/etc/issue",
            data = parts[open_idx]
            data = data.replace('open(', '')
            data = data.replace('"', '')
            data = data.replace(',', '')
            if data.endswith(filename):
                results.append(data)
        else:
            continue

    # return the last found
    if results:
        if not list:
            return results[-1]
        else:
            # we want all files in some cases
            return sorted(set(results))
    else:
        return None


def safequote(javacmd):

    """ Some JRE args need to be quoted """

    '''
    (Pdb) pp JAVACMD[0:10]
    ['/usr/lib/jvm/java-1.7.0-openjdk-1.7.0.51.x86_64/bin/java',
     '-Dproc_/root/test.hbase',
     '-XX:OnOutOfMemoryError=kill -9 %p',
     '-Xmx1000m',
     '-XX:+UseConcMarkSweepGC',
     '-XX:+UseParNewGC',
     '-XX:NewRatio=16',
     '-XX:CMSInitiatingOccupancyFraction=70',
     '-XX:+UseCMSInitiatingOccupancyOnly',
     '-XX:MaxGCPauseMillis=100']
    '''

    if type(javacmd) != list:
        return javacmd

    for idx, val in enumerate(javacmd):
        # '-XX:OnOutOfMemoryError=kill -9 %p'
        if ' ' in val and val.startswith('-X') and '=' in val and not val.endswith('"'):
            newval = val.split('=', 1)
            newval[1] = '"' + newval[1] + '"'
            newval = '='.join(newval)
            javacmd[idx] = newval

    #import pdb; pdb.set_trace()
    return javacmd


def javaverbose(options, CLASSPATH, JAVACMD, JAVAENV, piping=True, svckey=None, usetimeout=True, timeout=TIMEOUT, workdir=WORKDIR):

    """ Re-run a java cmd with -verbose:class """

    # Fix quoting around jvm options
    # http://gitlab.sas.com/prd-ctf/hadoop-integration-test/issues/16
    JAVACMD = safequote(JAVACMD)

    # inject -verbose:class
    JAVACMD.insert(1, "-verbose:class")

    # add timeout only if the caller allows and not already part of the command
    if usetimeout and not JAVACMD[0].endswith('/timeout'):
        if checkcmdinpath('timeout') and piping:
            timeoutcmd = getcmdpath('timeout')
            # Set timeout on the command
            JAVACMD.insert(0, TIMEOUT)
            JAVACMD.insert(0, "-s SIGKILL")
            JAVACMD.insert(0, timeoutcmd)
        else:
            JAVACMD.insert(0, bashtimeout(workdir))

    NEWCMD = "%s" % " ".join(JAVACMD)

    # fix -e string quoting for hive commands
    if " -e " in NEWCMD:
        TMPCMD = NEWCMD.split(' -e ')
        if not TMPCMD[1].startswith('"') and not TMPCMD[1].endswith('"'):
            TMPCMD[1] = '"%s"' % TMPCMD[1]
            NEWCMD = ' -e '.join(TMPCMD)

    # capture the rc
    NEWCMD += "\nRC=$?\nexit $RC\n"

    # Create the wrapper script
    fh = tempfile.NamedTemporaryFile(dir=WORKDIR, prefix='%s-verbose-' % svckey,  delete=False)
    fname = fh.name
    fh.write("#!/bin/bash\n")

    # Split the classpath export into multiple lines to avoid the
    # max command line length limitations.
    if CLASSPATH:
        CPS = CLASSPATH.split(':')
        for idx,x in enumerate(CPS):
            if idx == 0:
                fh.write('export CLASSPATH="%s"\n' % x)
            else:
                fh.write('export CLASSPATH="$CLASSPATH:%s"\n' % x)

    fh.write(NEWCMD)
    fh.close()

    if not options.verbose and piping:
        cmd = "bash -x %s" % fname
        p = Popen(cmd, cwd=WORKDIR, stdout=PIPE, stderr=PIPE, shell=True)
        so, se = p.communicate()
        rc = p.returncode
    elif not options.verbose and not piping:
        cmd = "bash %s" % fname

        # Define a filename to hold stdout+stderr
        outfile = fname + ".out"

        # Redirect the script to the filename
        cmd += " > %s 2>&1" % outfile
        p = Popen(cmd, cwd=WORKDIR, shell=True)
        so, se = p.communicate()
        rc = p.returncode

        # Read the outfile
        f = open(outfile, "rb")
        fdata = f.read()
        f.close()
        so = fdata
        se = ""

    else:
        cmd = "bash -x %s" % fname
        (rc, so, se) = run_command_live(cmd)

    if options.noclean or options.stoponerror:
        logfile = fname + '.log'
        f = open(logfile, "wb")
        f.write(str(so) + str(se))
        f.close()

    if rc != 0:
        LOG.error("%s - %s script failed, exit code %s" % (svckey, fname, rc))

        # if in verbose mode, display the error(s)
        if options.verbose:
            lines = str(so) + str(se)
            lines = lines.split('\n')
            lines = [x for x in lines if not '[load' in x.lower()]
            for line in lines:
                LOG.error("%s - %s" % (svckey, line))

        #if options.stoponerror:
        #    sys.exit(p.returncode)

    rawdata = so + se
    return (rc, rawdata)


def parseverboseoutput(rawdata):

    """ parse classpaths and jarfiles from -verbose:class output """
    classpaths = []

    for rl in rawdata.split("\n"):
        if rl.startswith("[Loaded") or rl.startswith('class load:'):
            # [Loaded java.. from /../rt.jar]
            # class load: org.ap.. from: file:../foo.jar
            parts = shlex.split(rl)
            if parts[1].lower() == 'load:':
                jfqn = parts[2]
            else:
                jfqn = parts[1]
            jarf = parts[-1].replace(']', '')

            if jarf.startswith('file:'):
                jarf = jarf.replace('file:', '')
            if not jarf.startswith('/'):
                continue

            if jarf.endswith('.jar'):
                classpaths.append((jfqn, jarf))

    # list of tuples
    #   [ (fqn, jarfile) ]
    return classpaths


def classpathstojars(classpaths):
    # convert a (cp,jar) tuple to a list of jars
    jars = []
    for fqn, jar in classpaths:
        if jar not in jars:
            jars.append(jar)
    jars = sorted(jars)
    return jars


def getversion(cmd, jarfiles):

    """ Find --version for a cli """

    version = _getversionstring(cmd)

    if version == None:
        jversions = []
        jardir = None
        jarname = None
        # resort to jarfilename chopping
        cjars = [x for x in jarfiles if cmd in x]
        for j in cjars:
            jf = os.path.basename(j)
            if jf.startswith(cmd):
                jardir = os.path.dirname(j)
                jarname = jf.replace('.jar', '')
                jv = jarname.split('-')
                ptr = None
                # find the index for the first numeric character
                for idx, val in enumerate(jv):
                    if hasattr(val, 'isdigit'):
                        if val.isdigit():
                            ptr = idx
                            break
                    if hasattr(val[0], 'isdigit'):
                        if val[0].isdigit():
                            ptr = idx
                            break
                    if type(val) == int:
                        ptr = idx
                        break
                if ptr:
                    thisv = '-'.join([str(x) for x in jv[ptr:]])
                    jversions.append(thisv)

        jversions = sorted(set(jversions))

        # check the release notes
        rver = None
        if jardir:
            if jardir.endswith('/lib'):
                cdir = jardir.replace('/lib', '')
                rn = os.path.join(cdir, "RELEASE_NOTES.txt")
                if os.path.isfile(rn):
                    f = open(rn, "rb")
                    data = f.readlines()
                    f.close()
                    parts = shlex.split(data[0])

                    if 'version' in data[0].lower():
                        parts = shlex.split(data[0])
                        if parts[5].lower() == 'version':
                            rver = parts[6]
                        elif parts[6].lower() == 'release.':
                            rver = parts[5]

        if len(jversions) == 1:
            if rver:
                if rver in jversions[0]:
                    version = jversions[0]
                else:
                    version = rver
            else:
                version = jversions[0]
        elif len(jversions) > 1:
            if rver:
                candidates = [x for x in jversions if rver in x]
                if len(candidates) == 1:
                    version = candidates[0]
        elif rver:
            version = rver

    return version


def _getversionstring(cmd):
    '''
    $ hadoop version
    Hadoop 2.3.0-cdh5.0.1

    $ hive --version
    Hive 0.13.0.2.1.2.1-471

    $ pig --version
    Apache Pig version 0.12.1.2.1.2.1-471 (rexported)
    '''
    version = None
    for v in ['--version', 'version']:
        vcmd = "%s %s" % (cmd, v)
        if cmd == "hive" and v == "version":
            # this would open an interactive shell
            continue
        LOG.info("%s" % vcmd)
        p = Popen(vcmd, cwd=WORKDIR, stdout=PIPE, stderr=PIPE, shell=True)
        so, se = p.communicate()
        rc = p.returncode

        if rc != 0:
            #continue
            pass
        else:
            lines = so.split('\n')
            l0 = shlex.split(lines[0].lower())
            if not l0:
                continue
            if l0[0] == cmd:
                #print "version = %s" % l0[1]
                version = l0[1]
                break
            elif l0[1] == cmd:
                #print "version = %s" % l0[3]
                version = l0[3]
                break
            break

    return version


def collecthiveinfo(workdir=WORKDIR, log=True, options=None):

    # Use hive's 'set -v' output to create a dict of active settings.
    #   runs as a singleton to reduce overall runtime
    # Also collect a list of tables in the default database.

    """
    env:USER=tdatuser
    env:WINDOWMANAGER=/usr/bin/icewm
    env:XCURSOR_THEME=
    env:XDG_CONFIG_DIRS=/etc/xdg
    env:XDG_DATA_DIRS=/usr/share:/etc/opt/kde3/share:/opt/kde3/share
    env:XFILESEARCHPATH=/usr/dt/app-defaults/%L/Dt
    env:XKEYSYMDB=/usr/share/X11/XKeysymDB
    env:XNLSPATH=/usr/share/X11/nls
    system:awt.toolkit=sun.awt.X11.XToolkit
    system:file.encoding=UTF-8
    system:file.encoding.pkg=sun.io
    system:file.separator=/
    system:hadoop.home.dir=/usr/lib/hadoop
    system:hadoop.id.str=tdatuser
    """

    hiveinfo = {}

    # Make this a singleton (except on re-run?)
    datafile = os.path.join(workdir, "hiveinfo")
    if not os.path.isfile(datafile):
        f = open(datafile, "wb")
        f.write("##RUNNING\n")
        f.close()
    else:
        # poll until file is finished
        status = "##RUNNING"
        data = []
        count = 0   # polling count
        stime = 10  # polling interval
        while status == "##RUNNING":
            if count > 0 and stime < 30:
                stime = stime * 2
            time.sleep(stime)
            f = open(datafile, "rb")
            data = f.readlines()
            if log:
                LOG.info("collecthiveinfo - [%s] status: %s" % (os.getpid(), data[0].strip()))
            f.close()
            if data[0].strip() == "##RUNNING":
                status = "##RUNNING"
            elif data[0].strip() == "##FINISHED":
                status = "##FINISHED"
            else:
                #FIXME ?
                status = "Other"
            count += 1

        # convert raw json data to a dict
        try:
            hiveinfo = json.loads(''.join(data[1:]))
        except Exception as e:
            if log:
                LOG.error("collecthiveinfo - EXCEPTION: %s" % e)
        if log:
            LOG.info("collecthiveinfo  - keys: %s" % hiveinfo.keys()[0:10])
        if hiveinfo:
            return hiveinfo

    LOG.info("collecthiveinfo - starting hive -e 'set -v'")
    hiveinfo = Tracer.gethivesetv(detectbeeline=True, log=True, workdir=workdir, options=None)
    LOG.info("collecthiveinfo - hive -e 'set -v' finished")


    '''
    # Get the list of tables
    hiveinfo['tables'] = []
    cmd = "%s -e 'show tables' 2>/dev/null" % hivecmd
    if log:
        LOG.info("collecthiveinfo - %s -e 'show tables' started" % hivecmd)
    (rc, so, se) = run_command(cmd, cwd=workdir)
    if log:
        LOG.info("collecthiveinfo - show tables finished")
    lines = so.split('\n')
    lines = [x.strip() for x in lines if x.strip()]
    for x in lines:
        hiveinfo['tables'].append(x)
    if log:
        LOG.info("collecthiveinfo - %s -e 'show tables' finished" % hivecmd)
    '''

    f = open(datafile, "wb")
    f.write("##FINISHED\n")
    f.write(json.dumps(hiveinfo))
    f.close()

    if log:
        LOG.info("collecthiveinfo - [%s] returning data", os.getpid())
    return hiveinfo



def commandinpstable(cmd):
    checkcmd = "ps aux | awk '{print $11}' | fgrep %s" % cmd
    (rc, so, se) = run_command(checkcmd)
    if rc == 0:
        return True
    else:
        return False


def get_local_environment():

    """ Data to display in the log for debugging purposes """

    info = {}

    # hostname
    info['hostname'] = socket.gethostname()

    # username
    info['username'] = getpass.getuser()

    # cwd
    info['pwd'] = os.getcwd()

    # checksum for this script
    sf = os.path.realpath(__file__)
    md5cmd = getcmdpath('md5sum')
    cmd = "%s %s | awk '{print $1}'" % (md5cmd, sf)
    (rc, so, se) = run_command(cmd, checkrc=False)
    info['script_md5'] = so.strip()

    # cli args
    info['script_args'] = sys.argv

    return info



############################################################
#   FILE MANAGEMENT
############################################################

def sitexmlcombiner(confdir, outfile="combined-site.xml"):

    # Verify the system has xml libs
    hasxml = False
    try:
        import xml.etree.ElementTree as ET
        hasxml = True
    except:
        pass

    if not hasxml:
        return False

    # Each property is stored here
    xdata = []
    ydata = []

    xhash = {}
    yhash = {}

    # clear out old copies
    outfile = os.path.join(confdir, outfile)
    merge = os.path.join(confdir, 'core-hdfs-merged.xml')

    if os.path.isfile(outfile):
        os.remove(outfile)

    # Read all site.xml files and parse them
    for file in os.listdir(confdir):
        fpath = os.path.join(confdir, file)
        if os.path.isfile(fpath) and fpath.endswith('.xml'):

                tree = ET.parse(fpath)
                root = tree.getroot()

                for child in root:
                    # create a dict for each propery
                    # and append to the overall list
                    xdict = {}
                    for x in child.getchildren():
                        xdict[x.tag] = x.text

                    # Skip empty tags
                    if not xdict:
                        continue

                    # Skip tags without names
                    if 'name' not in xdict:
                        continue

                    if xdict['name'] not in xhash:
                        xhash[xdict['name']] = len(xdata)
                        xdata.append(xdict)
                    else:
                        xdata[xhash[xdict['name']]] = xdict

                    if 'core-site.xml' in fpath or 'hdfs-site.xml' in fpath:
                        if xdict['name'] not in yhash:
                            yhash[xdict['name']] = len(ydata)
                            ydata.append(xdict)
                        else:
                            ydata[yhash[xdict['name']]] = xdict

    # Write out properties to a combined xml file
    f = open(outfile, "wb")
    h = open(merge,   "wb")

    f.write("<configuration>\n")
    h.write("<configuration>\n")

    for x in xdata:
        f.write("\t<property>\n")
        for k in sorted(x.keys()):
            if k == "description":
                continue
            f.write("\t\t<%s>%s</%s>\n" % (k, x[k] or '', k))
        f.write("\t</property>\n")
    f.write("</configuration>\n")

    for x in ydata:
        h.write("\t<property>\n")
        for k in sorted(x.keys()):
            if k == "description":
                continue
            h.write("\t\t<%s>%s</%s>\n" % (k, x[k] or '', k))
        h.write("\t</property>\n")
    h.write("</configuration>\n")

    return True


def xmlnametovalue(rawxml, name):

    """ Grab the value for a given xml node by name """

    # <name>hive.enforce.sorting</name>
    # <value>true</value>

    # clean up empty lines
    tl = [x.strip() for x in rawxml.split("\n") if x.strip()]
    this_idx = None
    # find line number for matching name
    for idx, val in enumerate(tl):
        if val == '<name>' + name + "</name>":
            this_idx = idx
    # get the value
    if this_idx:
        data = tl[this_idx + 1]
        if data.startswith('<value>'):
            data = data.replace('<value>', '')
        if data.endswith('</value>'):
            data = data.replace('</value>', '')
        return data
    return None




def copyjars(options, datadict):
    LOG.info("Evaluating found jars ...")
    jarfiles = []
    dest = options.dir
    for k, v in datadict.iteritems():
        if 'jarfiles' in v:
            if v['jarfiles']:
                for jf in v['jarfiles']:
                    LOG.info('%s requires %s' % (k, jf))
                    if jf not in jarfiles and not '/sas.' in jf:

                        finalpath = jf

                        # Get the version for this jar
                        (jf_name, jf_delimiter, jf_version) = \
                            Tracer.split_jar_name_and_version(os.path.basename(jf))

                        # Check for a versioned filename ...
                        if not jf_version:
                            jrp = os.path.realpath(jf)
                            if os.path.basename(jf) != os.path.basename(jrp):

                                (jrp_name, jrp_delimiter, jrp_version) = \
                                    Tracer.split_jar_name_and_version(os.path.basename(jrp))

                                if jrp_version:
                                    LOG.info("Copying from %s instead of %s" % (jrp, jf))
                                    finalpath = jrp

                        jarfiles.append(finalpath)

    # Prefer jar versions based on user's choice of filter ...
    if options.filterby:
        if options.filterby == "hadoop":
            jarfiles = Tracer.filter_jars_by_hadoop_classpath(jarfiles, verbose=True)
        elif options.filterby == "hive":
            (hive_dirs, hive_jars) = Tracer.gethiveclasspath()
            jarfiles = Tracer.filter_jars_by_inclasspath(jarfiles, filter=hive_jars)
        elif options.filterby == "hcat":
            (hcat_dirs, hcat_jars) = Tracer.run_and_parse_classpath(cmd="hcat -classpath")
            jarfiles = Tracer.filter_jars_by_inclasspath(jarfiles, filter=hcat_jars)
        elif options.filterby == "latest":
            jarfiles = Tracer.filter_jars_by_latest(jarfiles)
        elif options.filterby == "count":
            jarfiles = Tracer.filter_jars_by_count(jarfiles)

    assert not os.path.isfile(dest), \
        "%s is a file and jars cannot be copied here" % dest

    if not os.path.isdir(dest) and not os.path.isfile(dest):
        os.makedirs(dest)
    else:
        if not options.nooverwrite:
            LOG.info("emptying contents of %s" % (dest))
            shutil.rmtree(dest)
            os.makedirs(dest)

    LOG.info("Copying jars to %s" % dest)

    for jf in sorted(jarfiles):
        thisf = os.path.basename(jf)
        thisp = os.path.join(dest, thisf)
        if not os.path.isfile(thisp) and os.path.isfile(jf):
            LOG.info("copy %s to %s" % (jf, dest))
            try:
                shutil.copy(jf, thisp)
            except Exception as e:
                LOG.error("%s" % e)


def dedupejars(options):
    # delete duplicate jars by md5sum
    md5cmd = getcmdpath('md5sum')
    cmd = "%s *.jar" % md5cmd
    jardict = {}
    (rc, so, se) = run_command(cmd, checkrc=False, cwd=options.dir)

    if rc != 0:
        return False

    lines = so.split('\n')
    lines = [x for x in lines if x and x.endswith('.jar')]
    for line in lines:
        parts = shlex.split(line)
        md5 = parts[0]
        jar = parts[1]
        if md5 not in jardict:
            jardict[md5] = []
        jardict[md5].append(jar)

    for k, v in jardict.iteritems():
        if len(v) == 1:
            continue

        # keep the longest filename
        longest = v[0]
        for jf in v:
            if len(jf) > longest:
                longest = jf
        for jf in v:
            if jf != longest:
                delpath = os.path.join(options.dir, jf)
                LOG.info('%s duplicates %s, removed' % (jf, longest))
                os.remove(delpath)


def copyconfig(options, datadict):
    confiles = []
    dest = options.conf
    for k, v in datadict.iteritems():
        if 'sitexmls' in v:
            if v['sitexmls']:
                for sx in v['sitexmls']:
                    if sx not in confiles:
                        confiles.append(sx)

    assert not os.path.isfile(dest), \
        "%s is a file and site xmls cannot be copied here" % dest

    if not os.path.isdir(dest) and not os.path.isfile(dest):
        os.makedirs(dest)
    else:
        if not options.nooverwrite:
            LOG.info("emptying contents of %s" % (dest))
            shutil.rmtree(dest)
            os.makedirs(dest)

    for sx in sorted(confiles):

        # ignore None types
        if not sx:
            continue

        thisf = os.path.basename(sx)
        thisp = os.path.join(dest, thisf)
        if not os.path.isfile(thisp):
            LOG.info("copy %s to %s" % (sx, dest))
            try:
                shutil.copy(sx, thisp)
            except Exception as e:
                LOG.error("%s" % e)


############################################################
#   EXECUTION MODIFIERS
############################################################

def checkprereqs():
    if not checkcmdinpath("strace"):
        print "Please install strace (yum install strace) before using this script"
        sys.exit(1)


def converge_services():
    """ Make all values in SERVICES dicts """

    for k in SERVICES.keys():
        if type(SERVICES[k]) == str:
            cmd = SERVICES[k]
            SERVICES[k] = {}
            SERVICES[k]['cmd'] = cmd
            SERVICES[k]['pre'] = None
            SERVICES[k]['post'] = None
        elif type(SERVICES[k]) == dict:
            if not 'pre' in SERVICES[k]:
                SERVICES[k]['pre'] = None
            if not 'post' in SERVICES[k]:
                SERVICES[k]['post'] = None


def add_hadoop_mr1_filter(filterlist):

    '''
    [root@jt-cdh526-0 ~]# hadoop version
    Hadoop 2.5.0-cdh5.2.6
    Subversion http://github.com/cloudera/hadoop -r 0f9d7616910ea5b3d843ad7a585319ccee7ccf61
    Compiled by jenkins on 2015-06-27T01:55Z
    Compiled with protoc 2.5.0
    From source with checksum adcadfe2cd17d440468156437a9bd7d
    This command was run using /opt/cloudera/parcels/.../jars/hadoop-common-2.5.0-cdh5.2.6.jar
    '''

    '''
    Hadoop 1.0.3
    Subversion http://mapr.com -r 5e1324a15a239bea726a5b7847cb3876c31b5035
    Compiled by root on Wed Feb  5 15:46:24 PST 2014
    From source with checksum 666d80fe70dbce9c1c636f604d80e591
    This command was run using /opt/mapr/hadoop/hadoop-0.20.2/lib/hadoop-0.20.2-dev-core.jar
    '''

    '''
    Hadoop V100R001C00
    Subversion git@rnd-git.huawei.com:datasight/hadoop2-4.git -r ba5dbd...f2f823c0f
    Compiled by dsbuild on 2015-05-26 20:00:06
    Compiled with protoc 2.5.0
    From source with checksum aaad529f1796e0dea1f38178d4f6e2
    '''

    hadoop_version = getversion("hadoop", [])
    if hadoop_version:

        if '.' in hadoop_version:
            hadoop_major_version = int(hadoop_version.split('.')[0])
        else:
            # huawei V100R001C00 ... assume >= 2.0
            hadoop_major_version = 2

        LOG.info("Hadoop major version = %s" % hadoop_major_version)
        if hadoop_major_version > 1:
            LOG.info("Hadoop version %s is > 1, adding MR1 filter" % hadoop_major_version)
            filterlist.append('org/apache/hadoop/mapred/JobStatus$1.class')

    return filterlist


def toggle_hivejdbc_or_beeline():

    ''' If beeline is available, exclude hivejdbc '''

    global SERVICES

    beeline = getcmdpath('beeline')

    if not beeline:

        # Is beeline next to hive? (mapr)
        hive = getcmdpath('hive')
        if hive:
            if os.path.islink(hive):
                hive = os.path.realpath(hive)

            bindir = os.path.dirname(hive)
            beelinecmd = os.path.join(bindir, "beeline")
            if os.path.isfile(beelinecmd):
                LOG.info('beeline found at %s' % beelinecmd)
                beeline = beelinecmd

    if not beeline:
        LOG.error("beeline is not in this users path")
    else:
        LOG.info("Excluding the hivejdbc tracer")
        SERVICES.pop("hivejdbc", None)



############################################################
#   Workflow functions
############################################################

def nothread_worker(svckey):

    """ Worker for both serial and parallel tracer """

    # Write a lock file to help determine what tracers are actively running
    lockfile = os.path.join(WORKDIR, "%s.running" % svckey.replace(' ', ''))
    f = open(lockfile, 'wb')
    f.write('')
    f.close()

    svc = SERVICES[svckey]
    cmd = None
    cmdclass = None
    if 'cmd' in svc:
        cmd = svc['cmd']
    if 'class' in svc:
        cmdclass = svc['class']

    rdict = {   'JRE': None,
                'CLASSPATH': None,
                'JAVACMD': None,
                'JAVAENV': None,
                'ECLASSPATH': None,
                'EJARS': None,
                'version': None,
                'sitexmls': None,
                'vrc': None,
                'stracerc': None,
                'metadata': {},
                'rc.cmd_strace': None,
                'rc.java_verbose': None,
                'jre': None,
                'classpath': None,
                'javacmd': None,
                'javaenv': None,
                'fqns': None,
                'jarfiles': None
            }

    XC = None
    if cmdclass:
        # Use the custom tracer classes
        LOG.info("%s - calling class %s" % (svckey, cmdclass))
        try:
            XC = eval(cmdclass + '()')
        except NameError as e:
            LOG.info("%s - %s" % (svckey, e))

    else:
        # Use the generic tracer class for anything else
        LOG.info("%s - calling class Tracer" % svckey)
        try:
            XC = eval("Tracer()")
        except NameError as e:
            LOG.info("%s - %s" % (svckey, e))

        # Fill in the necessary attributes
        XC.svckey = svckey
        XC.tracecmd = cmd
        XC.precmd = svc['pre']
        XC.postcmd = svc['post']

    # Call the Run() method to begin the tracing ...
    if XC:
        try:
            XC.options = options
            XC.SetWorkdir(WORKDIR)
            XC.Run()

            rdict['rc.cmd_strace'] = XC.rc_strace
            rdict['rc.java_verbose'] = XC.rc_verbose
            rdict['version'] = XC.version
            rdict['jre'] = XC.jre
            rdict['classpath'] = XC.classpath
            rdict['javacmd'] = XC.javacmd
            rdict['javaenv'] = XC.javaenv
            rdict['fqns'] = XC.fqns
            rdict['jars'] = XC.jars
            rdict['jarfiles'] = XC.jarfiles
            rdict['sitexmls'] = XC.sitexmls
            rdict['metadata'] = XC.metadata
        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            tbtext = ''.join(traceback.format_exception(exc_type,
                                exc_value, exc_traceback, 10))
            LOG.info("%s - Exception: %s %s" % (svckey, e, tbtext))

    # Cleanup the lock
    os.remove(lockfile)

    return rdict


def threaded_worker(input, output, options):

    """ Worker thread for the parallel tracer """

    for svckey in iter(input.get, 'STOP'):
        # ~run
        rdict = nothread_worker(svckey)
        # ~return
        output.put((svckey, rdict))


def threaded_tracer(options):

    """ Get tracer results for all services in parallel mode """

    datadict = {}
    NUMBER_OF_PROCESSES = len(SERVICES.keys())

    # Create queues
    task_queue = Queue()
    done_queue = Queue()

    # Submit tasks
    for k in SERVICES.keys():
        task_queue.put(k)

    # Start workers
    for i in range(NUMBER_OF_PROCESSES):
        Process(target=threaded_worker, args=(task_queue, done_queue, options)).start()

    # Collect results
    results = []
    for i in range(NUMBER_OF_PROCESSES):
        results.append(done_queue.get())

    for i in range(NUMBER_OF_PROCESSES):
        task_queue.put('STOP')

    for r in results:
        try:
            svc = r[0]
            datadict[svc] = {}
            datadict[svc] = r[1]

        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            LOG.info("Traceback: %s, %s" % (e, exc_traceback))

    return datadict


def nothread_tracer(options, rerun=False, timeout=TIMEOUT):

    """ Get tracer results for all services in serial mode """

    global SERVICES
    global DATACACHE
    datadict = {}

    for svc, cmd in SERVICES.iteritems():

        rdict = nothread_worker(svc)
        datadict[svc] = rdict

    return datadict


def fix_datadict(datadict):

    ''' Fix miscellaneous issues in the datadict '''

    # Get rid of any newlines in classpaths
    #   http://sww.sas.com/cgi-bin/quick_browse?defectid=S1207561
    for k,v in datadict.iteritems():
        if 'classpath' in v:
            if v['classpath']:
                if '\n' in v['classpath']:
                    datadict[k]['classpath'] = v['classpath'].replace('\n', '')

    return datadict


def write_hadooptracer_json(options, localinfo, datadict):

    #####################################
    #   JSON WRITER ...
    #####################################
    datadict = fix_datadict(datadict)

    thisfile = "/tmp/hadooptrace.json"
    if options:
        if hasattr(options, 'filename'):
            thisfile = options.filename
    LOG.info("Writing results to %s" % thisfile)

    # strip out the dirname
    dirpath = os.path.dirname(thisfile)
    if not os.path.isdir(dirpath):
        os.makedirs(dirpath)

    # add local data
    if not 'metadata' in datadict:
        datadict['tracer_metadata'] = {}
    for k,v in localinfo.iteritems():
        datadict['tracer_metadata'][k] = v

    f = open(thisfile, "wb")
    f.write(json.dumps(datadict, sort_keys=True, indent=2))
    f.close()


############################################################
#   MAIN
############################################################

def main(options=None):

    # do not run if things are missing
    checkprereqs()

    global SERVICES
    global DATACACHE
    global TIMEOUT
    global WORKDIR
    global LOG

    # Override the base directory if specified
    WORKDIR_BAK = WORKDIR
    if options.tmpdir:
        options.logfile = os.path.join(options.tmpdir, os.path.basename(options.logfile))
        options.conf = os.path.join(options.tmpdir, os.path.basename(options.conf))
        options.dir = os.path.join(options.tmpdir, os.path.basename(options.dir))
        options.filename = os.path.join(options.tmpdir, os.path.basename(options.filename))

        if not os.path.isdir(options.tmpdir):
            os.makedirs(options.tmpdir)
        WORKDIR_BAK = WORKDIR
        WORKDIR = tempfile.mkdtemp(prefix="hadooptracer.", dir=options.tmpdir)
    else:
        WORKDIR = tempfile.mkdtemp(prefix="hadooptracer.")

    # Fixup the tmp file locations in some of the older tracers
    for k,v in SERVICES.iteritems():
        if 'pre' in v:
            if v['pre']:
                SERVICES[k]['pre'] = v['pre'].replace(WORKDIR_BAK, WORKDIR)
        if 'cmd' in v:
            if v['cmd']:
                SERVICES[k]['cmd'] = v['cmd'].replace(WORKDIR_BAK, WORKDIR)
        if 'post' in v:
            if v['post']:
                SERVICES[k]['post'] = v['post'].replace(WORKDIR_BAK, WORKDIR)

    if not os.path.isdir(WORKDIR):
        os.makedirs(WORKDIR)

    # Create a file appender for the logger
    fhdlr = logging.FileHandler(options.logfile)
    formatter = logging.Formatter('%(asctime)s hadooptracer [%(levelname)s] %(message)s')
    fhdlr.setFormatter(formatter)
    LOG.addHandler(fhdlr)

    LOG.info("Hadoop Tracer started")
    LOG.info("Temporary File Directory: %s" % WORKDIR)

    # Log details about the environment
    localinfo = get_local_environment()
    keys = sorted(localinfo.keys())
    for k in keys:
        LOG.info("%s - %s" % (k,localinfo[k]))
    LOG.info("")

    # Ignore yarn tracers if this is an MR1 cluster
    if not getcmdpath('yarn'):
        if options.svckey:
            if 'yarn-node' not in options.svckey:
                SERVICES.pop('yarn-node', None)
            if 'yarn-apps' not in options.svckey:
                SERVICES.pop('yarn-apps', None)
        else:
            SERVICES.pop('yarn-node', None)
            SERVICES.pop('yarn-apps', None)

    # Add MR1 exclusions if this is 2.x ...
    if not options.noexclusions:
        options.excludepackage = add_hadoop_mr1_filter(options.excludepackage)

    if options.listsvckeys:
        pprint(SERVICES)
        return 0
    elif options.svckey:
        #import pdb; pdb.set_trace()
        tmpsvcs = {}
        for k in options.svckey:
            tmpsvcs[k] = SERVICES[k]

        #SERVICES = {options.svckey: SERVICES[options.svckey]}
        SERVICES = tmpsvcs
        #import pdb; pdb.set_trace()

    elif options.excludesvckey:
        for k in options.excludesvckey:
            if k in SERVICES:
                SERVICES.pop(k, None)

    if options.command:
        key = shlex.split(options.command)[0]
        SERVICES = {key: options.command}

    # Only use hivejdbc if no beeline or allowed by user ...
    if not options.command \
        and 'hivejdbc' in SERVICES \
        and not options.nohivejdbctoggle:

        if options.svckey:
            if not 'hivejdbc' in options.svckey:
                toggle_hivejdbc_or_beeline()
        else:
            toggle_hivejdbc_or_beeline()

    converge_services()

    # Wait till all other tracers are finished before running mapreduce
    # it seems as though a single MR job can cause all other tracers
    # to hang up on the backend calls (especially on a mapr sandbox)
    MRSERVICES = None
    if not options.nothreads and 'mapreduce' in SERVICES and len(SERVICES.keys()) > 1:
        MRSERVICES = copy.deepcopy(SERVICES)
        SERVICES.pop('mapreduce', None)

    # trace defined commands threaded or not threaded
    if not options.nothreads:
        LOG.info("Starting parallel tracing")
        datadict = threaded_tracer(options)
        LOG.info("Finished with parallel tracing")
    else:
        LOG.info("Starting serialized tracing")
        datadict = nothread_tracer(options)

    # Run mapreduce now
    if not options.nothreads and MRSERVICES:
        LOG.info("Running just the mapreduce tracer now in serial mode ...")
        SERVICES = {}
        SERVICES['mapreduce'] = MRSERVICES['mapreduce']
        mrdict = nothread_tracer(options)
        # copy the results back to the main dict
        datadict['mapreduce'] = copy.deepcopy(mrdict['mapreduce'])
        # fix the services dict
        SERVICES = copy.deepcopy(MRSERVICES)

    # Only use hadoop classpath if tracing hadoop
    if not options.nohadoopclasspath:
        if (not options.svckey and not options.command) \
            or ("hadoop" in SERVICES) or ("hadoop-put" in SERVICES):

            LOG.info("Check 'hadoop classpath' command output")
            hcpjars = hadoopclasspathcmd()
            datadict['hadoop-classpath'] = {}
            datadict['hadoop-classpath']['rc.cmd_strace'] = 0
            datadict['hadoop-classpath']['rc.java_verbose'] = 0
            datadict['hadoop-classpath']['jarfiles'] = hcpjars

    # Some poorly provisioned clusters (such as sandboxes)
    # have issues with concurrency, so various tracers will
    # fail for no good reason. Due to that "problem", attempt
    # to rerun those tracers in serialized mode.
    if not options.skipretry:
        LOG.info("looking for failures to re-run those tracers")
        keys = datadict.keys()
        failed_keys = []
        for k,v in datadict.iteritems():
            if not 'rc.cmd_strace' in v or not 'rc.java_verbose' in v:
                failed_keys.append(k)
            elif v['rc.cmd_strace'] != 0 or v['rc.java_verbose'] != 0:
                failed_keys.append(k)
        LOG.info("retracing: %s" % failed_keys)

        # save the traced data to avoid re-running strace
        DATACACHE = copy.deepcopy(datadict)

        # save the global services dict
        for key in keys:
            if key not in failed_keys:
                SERVICES.pop(key, None)

        retry_dict = nothread_tracer(options, rerun=True)

        # merge the new data back into the datadict
        for key in failed_keys:
            if key in retry_dict:
                datadict[key] = copy.deepcopy(retry_dict[key])
            else:
                datadict[key] = {}
                datadict[key]['rc.cmd_strace'] = -1
                datadict[key]['rc.java_verbose'] = -1

    #####################################
    #   JSON WRITER ...
    #####################################
    write_hadooptracer_json(options, localinfo, datadict)

    #LOG.info("Copy jars to %s" % options.dir)
    copyjars(options, datadict)

    LOG.info("Deduplicating jars")
    dedupejars(options)

    LOG.info("Copy site.xml files to %s" % options.conf)
    copyconfig(options, datadict)

    LOG.info("Combine site.xml files into combined-site.xml")
    sitexmlcombiner(options.conf)

    if options.noclean:
        LOG.info("Tempfiles stored in %s" % WORKDIR)
    else:
        LOG.info("Cleaning up temporary directory %s" % WORKDIR)
        shutil.rmtree(WORKDIR)

    # create the returncode
    rc = 0
    failed = ''
    for k, v in datadict.iteritems():
        if k == "tracer_metadata":
            continue
        if 'rc.cmd_strace' in v:
            if v['rc.cmd_strace'] != 0:
                fk = k + '.rc.cmd_strace '
                failed += fk
                rc += 1
        else:
            fk = k + '.rc.cmd_strace '
            failed += fk
            rc += 1
        if 'rc.java_verbose' in v:
            if v['rc.java_verbose'] != 0:
                fk = k + '.rc.java_verbose '
                failed += fk
                rc += 1
        else:
            fk = k + '.rc.java_verbose '
            failed += fk
            rc += 1

    if options.stoponerror:
        LOG.info("Hadoop Tracer finished - failures: %s [%s]" % (rc, str(failed)))
        return rc
    else:
        if rc == 0:
            LOG.info("Hadoop Tracer finished - failures: %s" % rc)
        else:
            LOG.info("Hadoop Tracer finished - failures: %s [ignored - %s]" % (rc, str(failed)))
        return 0



if __name__ == "__main__":

    parser = OptionParser()

    # Results Storage
    parser.add_option("-b", "--basedir",
                      help="Use this directory instead of /tmp for storing all results",
                      action="store", dest="tmpdir")
    parser.add_option("-f", "--file",
                      dest="filename",
                      help="write results to FILE",
                      default="/tmp/hadooptracer.json",
                      metavar="FILE")
    parser.add_option("-d", "--directory", "--jars",
                      help="copy jars to this directory",
                      default="/tmp/jars",
                      action="store", dest="dir")
    parser.add_option("--conf", "--confdirectory", "--sitexmls",
                      help="copy config files to this directory",
                      default="/tmp/sitexmls",
                      action="store", dest="conf")
    parser.add_option("--logfile",
                      help="Store the log output here",
                      default="/tmp/hadooptracer.log",
                      action="store", dest="logfile")

    # Hive settings
    parser.add_option("-s", "--hivehostname",
                      help="specify the hive host name if its not namenode.",
                      default=None,
                      action="store", dest="hivehost")
    parser.add_option("--hiveusername",
                      help="specify the hive username if necessary.",
                      default="%s" % getpass.getuser(),
                      action="store", dest="hiveusername")
    parser.add_option("--hivepassword",
                      help="specify the hive username if necessary.",
                      default="",
                      action="store", dest="hivepassword")
    parser.add_option("--hivejdbcurl",
                      help="Beeline/JDBC connection string",
                      default=None,
                      action="store", dest="hivejdbcurl")

    # Limit traced commands
    parser.add_option("--listsvckeys",
                      help="list services that will be traced",
                      default=False,
                      action="store_true", dest="listsvckeys")

    parser.add_option("--svckey",
                      help="trace only this service key [see list keys]",
                      action="append", dest="svckey")

    parser.add_option("--excludesvckey",
                      help="trace only this service key [see list keys]",
                      action="append", dest="excludesvckey")

    parser.add_option("--command",
                      help="trace this arbitrary command",
                      action="store", dest="command")

    parser.add_option("--nohadoopclasspath", action="store_true",
                      default=False,
                      help="do not fetch all the jars in the hadoop classpath")

    parser.add_option("--nohivejdbctoggle", action="store_true",
                      default=False,
                      help="do not skip hivejdbc if beeline exists")

    # General behavior controls
    parser.add_option("--nooverwrite", action="store_true",
                      default=False,
                      dest="nooverwrite",
                      help="do not clean out pre-existing jar/conf dirs")

    parser.add_option("--noclean", action="store_true",
                      help="do not clean up temp files")

    parser.add_option("--stoponerror", action="store_true",
                      help="If a command fails, change the file exit code to non-zero [default: False]")

    parser.add_option("--nothreads", action="store_true",
                      help="Do not multi-thread the tracing actions")

    parser.add_option("--verbose", action="store_true",
                      default=False,
                      help="Show extended information in the log output")

    parser.add_option("--skipretry",
                      action="store_true",
                      default=False,
                      help="Do not retry tracers that failed (serialized) [default:false]")

    parser.add_option("--filterby",
                      default=None,
                      help="Filter final jars by either hadoop|hcat|hive|latest|count")

    # Dataloader+derby workaround:
    #   https://rndjira.sas.com/browse/VAPPEN-1775
    #   http://sww.sas.com/ds/DefectsSearch/S1186/S1186811.html
    #   http://sww.sas.com/cgi-bin/quick_browse?defectid=S1186807
    parser.add_option("--excludepackage",
                      help="exclude any jars that contain these packages [should be a / delimited classpath]",
                      default=['org/apache/derby'],
                      action="append", dest="excludepackage")

    parser.add_option("--skipexclusions", action="store_true",
                      dest="noexclusions",
                      default=False,
                      help="Do not make jar exclusions.")

    (options, args) = parser.parse_args()

    # optparse is finicky on some machines.
    if '--skipexclusions' in sys.argv:
        options.noexclusions = True
    if '--nofilter' in sys.argv:
        options.nofilter = True

    sys.exit(main(options=options))
