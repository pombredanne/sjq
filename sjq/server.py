import os
import sys
import select
import socket
import tempfile
import threading
import subprocess
import SocketServer

import sjq.config
import handler
import jobqueue

class SJQServer(object):

    def __init__(self, verbose=False, args=None):
        self.config = sjq.config.load_config(args)

        self.job_queue = jobqueue.JobQueue(self.config['sjq.db'])

        self._server = None
        self._is_shutdown = False
        self.verbose = verbose

        self.cond = threading.Condition()

    def sched(self):
        procs_avail = self.config['sjq.maxprocs']
        mem_avail = self.config['sjq.maxmem']

        running_processes = []

        while not self._is_shutdown:
            print "-----------"
            print "PROC AVAIL: %s" % procs_avail
            print "MEM AVAIL: %s" % (mem_avail if mem_avail else "*")
            removelist = []
            for i, (proc, job) in enumerate(running_processes):
                retcode = proc.poll()
                if retcode is not None:
                    print "JOB: %s PID: %s DONE (%s)" % (job['jobid'], proc.pid, retcode)
                    if retcode == 0:
                        self.job_queue.update_job_state(job['jobid'], 'S', retcode)
                    else:
                        self.job_queue.update_job_state(job['jobid'], 'F', retcode)
                        self.job_queue.abort_deps(job['jobid'])

                    procs_avail += job['procs']
                    if mem_avail is not None:
                        mem_avail += job['mem']
                    removelist.insert(0,i)
                else:
                    print "JOB: %s PID: %s RUNNING" % (job['jobid'], proc.pid)


            for i in removelist:
                del running_processes[i]


            print 'Checking held jobs...'
            self.job_queue.check_held_jobs()
            print 'Looking for jobs...'
            job = self.job_queue.findjob(procs_avail, mem_avail)
            if job:
                proc = self.spawn_job(job)
                if proc:
                    print "JOB: %s PID: %s STARTED" % (job['jobid'], proc.pid)
                    self.job_queue.update_job_state(job['jobid'], 'R')
                    procs_avail -= job['procs']
                    if mem_avail is not None:
                        mem_avail -= job['mem']
                    running_processes.append((proc, job))
                    continue

            self.job_queue.close()  # close the connection for the thread, if opened
            self.cond.acquire()
            self.cond.wait(10)
            self.cond.release()

    def spawn_job(self, job):
        cmd = None
        for line in [x.strip() for x in job['src'].split('\n')]:
            if line[:2] == '#!':
                cmd = line[2:]
            break

        if not cmd:
            sys.stderr.write("Don't know how to run job: %s\n" % line)
        else:
            if not 'cwd' in job or not job['cwd']:
                cwd = os.path.expanduser("~")
            else:
                cwd = job['cwd']

            if job['stdout']:
                if job['stdout'][0] == '/':
                    stdout = open(job['stdout'], 'w')
                else:
                    stdout = open(os.path.join(job['cwd'], job['stdout']), 'w')
            else:
                stdout = open(os.path.join(job['cwd'], '%s.o%s' % (job['name'], job['jobid'])), 'w')

            if job['stderr']:
                if job['stderr'][0] == '/':
                    stderr = open(job['stderr'], 'w')
                else:
                    stderr = open(os.path.join(job['cwd'], job['stderr']), 'w')
            else:
                stderr = open(os.path.join(job['cwd'], '%s.e%s' % (job['name'], job['jobid'])), 'w')

            env = None

            env = {}
            if 'env' in job and job['env']:
                for pair in job['env'].split(','):
                    k,v = pair.split('=',1)
                    env[k]=v

            env['JOB_ID'] = str(job['jobid'])

            if not 'uid' in job:
                job['uid'] = None
            if not 'gid' in job:
                job['gid'] = None

            stdin = tempfile.TemporaryFile()
            stdin.write(job['src'])
            stdin.seek(0)

            if os.getuid() == 0:
                preexec_fn = demote(job['uid'], job['gid'])
            else:
                preexec_fn = None

            proc = subprocess.Popen([cmd], stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd, env=env, preexec_fn=preexec_fn)
            return proc

        return None


    def start(self):
        if self._is_shutdown:
            sys.stderr.write("SJQ server already shutdown!")
            return

        if os.path.exists(self.config['sjq.socket']):
            sys.stderr.write("Socket path: %s exists!" % self.config['sjq.socket'])
            return

        if not self._server:
            sys.stderr.write("Starting job scheduler\n")
            t = threading.Thread(target=self.sched, args = ())
            t.daemon = True
            t.start()

            self._server = ThreadedUnixServer(self.config['sjq.socket'], handler.SJQHandler)
            self._server.socket.settimeout(30.0)
            self._server.sjq = self
            sys.stderr.write("SQJ - listening for job requests...\n")
            try:
                self._server.serve_forever()
            except socket.error: 
                pass
            except select.error:
                pass
            except KeyboardInterrupt:
                sys.stderr.write("\n")

            self.__shutdown()
            t.join()

    def shutdown(self):
        # if you don't do this, then we'll deadlock
        self._server.socket.close()

    def __shutdown(self):
        if self._server:
            # self.lock.acquire()

            sys.stderr.write("Shutting down...")
            try:
                self._server.shutdown()
            except:
                pass
            sys.stderr.write(" OK\n")

            sys.stderr.write("Removing socket...")
            try:
                os.unlink(self.config['sjq.socket'])
            except:
                pass
            sys.stderr.write(" OK\n")

            sys.stderr.write("Closing job queue...")
            sys.stderr.write(" OK\n")

            self._server = None
            self._is_shutdown = True

            self.cond.acquire()
            self.cond.notify()
            self.cond.release()
            # self.lock.release()

    def submit_job(self, src, procs=None, mem=None, **args):
        if procs == None:
            procs = self.config['sjq.defaults.procs']
        
        if mem == None:
            mem = self.config['sjq.defaults.mem']
        else:
            mem = sjq.convert_mem_val(mem)

        if procs > self.config['sjq.maxprocs']:
            return None

        if self.config['sjq.maxmem'] and mem > self.config['sjq.maxmem']:
            return None

        args['procs'] = procs
        args['mem'] = mem
        args['src'] = src

        jobid = self.job_queue.submit(args)
        self.job_queue.close()

        self.cond.acquire()
        self.cond.notify()
        self.cond.release()
        return jobid


def demote(uid, gid):
    def wrap():
        if gid is not None:
            os.setgid(gid)
        if uid is not None:
            os.setuid(uid)
    return wrap

class ThreadedUnixServer(SocketServer.ThreadingMixIn, SocketServer.UnixStreamServer):
    pass
