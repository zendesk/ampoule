import math
from cStringIO import StringIO as sio

from twisted.internet import error, defer
from twisted.python import failure, reflect
from twisted.trial import unittest
from twisted.protocols import amp
from ampoule import main, child, commands, pool

class ShouldntHaveBeenCalled(Exception):
    pass

def _raise(_):
    raise ShouldntHaveBeenCalled(_)

class _FakeT(object):
    closeStdinCalled = False
    def __init__(self, s):
        self.s = s

    def closeStdin(self):
        self.closeStdinCalled = True

    def write(self, data):
        self.s.write(data)

class FakeAMP(object):
    connector = None
    reason = None
    def __init__(self, s):
        self.s = s
        
    def makeConnection(self, connector):
        if self.connector is not None:
            raise Exception("makeConnection called twice")
        self.connector = connector
    
    def connectionLost(self, reason):
        if self.reason is not None:
            raise Exception("connectionLost called twice")
        self.reason = reason
    
    def dataReceived(self, data):
        self.s.write(data)

class Ping(amp.Command):
    arguments = [('data', amp.String())]
    response = [('response', amp.String())]

class Pong(amp.Command):
    arguments = [('data', amp.String())]
    response = [('response', amp.String())]

class Pid(amp.Command):
    response = [('pid', amp.Integer())]

class Child(child.AMPChild):
    def ping(self, data):
        return self.callRemote(Pong, data=data)
    Ping.responder(ping)

class PidChild(child.AMPChild):
    def pid(self):
        import os
        return {'pid': os.getpid()}
    Pid.responder(pid)

class First(amp.Command):
    arguments = [('data', amp.String())]
    response = [('response', amp.String())]

class Second(amp.Command):
    pass

class WaitingChild(child.AMPChild):
    deferred = None
    def first(self, data):
        self.deferred = defer.Deferred()
        return self.deferred.addCallback(lambda _: {'response': data})
    First.responder(first)
    def second(self):
        self.deferred.callback('')
        return {}
    Second.responder(second)

class Die(amp.Command):
    pass

class BadChild(child.AMPChild):
    def die(self):
        self.shutdown = False
        self.transport.loseConnection()
        return {}
    Die.responder(die)

class TestAMPConnector(unittest.TestCase):
    def _makeConnector(self, s, sa):
        a = FakeAMP(sa)
        ac = main.AMPConnector(a)
        assert ac.name is not None
        ac.transport = _FakeT(s)
        return ac
        
    def test_protocol(self):
        """
        Test that outReceived writes to AMP and that it triggers the
        finished deferred once the process ended.
        """
        s = sio()
        sa = sio()
        ac = self._makeConnector(s, sa)
        
        for x in xrange(99):
            ac.outReceived(str(x))
        
        ac.processEnded(failure.Failure(error.ProcessDone(0)))
        return ac.finished.addCallback(
            lambda _: self.assertEqual(sa.getvalue(), ''.join(str(x) for x in xrange(99)))
        )
        
    def test_protocol_failing(self):
        """
        Test that a failure in the process termination is correctly
        propagated to the finished deferred.
        """
        s = sio()
        sa = sio()
        ac = self._makeConnector(s, sa)
        
        ac.finished.addCallback(_raise)
        fail = failure.Failure(error.ProcessTerminated())
        self.assertFailure(ac.finished, error.ProcessTerminated)
        ac.processEnded(fail)

    def test_startProcess(self):
        """
        Test that startProcess actually starts a subprocess and that
        it receives data back from the process through AMP.
        """
        STRING = "ciao"
        s = sio()
        a = FakeAMP(s)
        BOOT = """\
import sys
def main(arg):
    sys.stdout.write(arg)
main(sys.argv[1])
"""
        amp, finished = main.startProcess(main.AMPConnector(a),
                                          STRING, bootstrap=BOOT,
                                          packages=("ampoule", "twisted"))
        def _eb(reason):
            print reason
        finished.addErrback(_eb)
        return finished.addCallback(lambda _: self.assertEquals(s.getvalue(), STRING))
    
    def test_failing_deferToProcess(self):
        """
        Test failing subprocesses and the way they terminate and preserve
        failing information.
        """
        STRING = "ciao"
        s = sio()
        a = FakeAMP(s)
        BOOT = """\
import sys
def main(arg):
    raise Exception(arg)
main(sys.argv[1])
"""
        ready, finished = main.startProcess(main.AMPConnector(a),
                                            STRING, bootstrap=BOOT,
                                            packages=("ampoule", "twisted"))
        self.assertFailure(finished, error.ProcessTerminated)
        finished.addErrback(lambda reason: self.assertEquals(reason.getMessage(), STRING))
        return finished

    def test_startAMPProcess(self):
        """
        Test that you can start an AMP subprocess and that it correctly
        accepts commands and correctly answers them.
        """
        STRING = "ciao"
        c, finished = main.startAMPProcess(child.AMPChild, packages=('ampoule', 'twisted'))
        c.callRemote(commands.Echo, data=STRING
           ).addCallback(lambda response:
                self.assertEquals(response['response'], STRING)
           ).addCallback(lambda _: c.callRemote(commands.Shutdown))
        return finished

    def test_startAMPAndParentProtocol(self):
        """
        Test that you can start an AMP subprocess and the children can
        call methods on their parent.
        """
        DATA = "CIAO"
        APPEND = "123"

        class Parent(amp.AMP):
            def pong(self, data):
                return {'response': DATA+APPEND}
            Pong.responder(pong)
        
        subp, finished = main.startAMPProcess(Child, ampParent=Parent, packages=('ampoule', 'twisted'))
        subp.callRemote(Ping, data=DATA
           ).addCallback(lambda response:
                self.assertEquals(response['response'], DATA+APPEND)
           ).addCallback(lambda _: subp.callRemote(commands.Shutdown))
        return finished

    def test_roundtripError(self):
        """
        Test that invoking a child using an unreachable class raises
        a L{RunTimeError} .
        """
        class Child(child.AMPChild):
            pass
        
        self.assertRaises(RuntimeError, main.startAMPProcess, Child, packages=('ampoule', 'twisted'))

class TestProcessPool(unittest.TestCase):
    def test_startStopWorker(self):
        """
        Test that starting and stopping a worker keeps the state of
        the process pool consistent.
        """
        pp = pool.ProcessPool()
        self.assertEquals(pp.started, False)
        self.assertEquals(pp.finished, False)
        self.assertEquals(pp.processes, set())
        self.assertEquals(pp._finishCallbacks, {})

        def _checks():
            self.assertEquals(pp.started, False)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), 1)
            self.assertEquals(len(pp._finishCallbacks), 1)
            return pp.stopAWorker()
        
        def _closingUp(_):
            self.assertEquals(pp.started, False)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), 0)
            self.assertEquals(pp._finishCallbacks, {})
        pp.startAWorker()
        return _checks().addCallback(_closingUp).addCallback(lambda _: pp.stop())

    def test_startAndStop(self):
        """
        Test that a process pool's start and stop method create the
        expected number of workers and keep state consistent in the
        process pool.
        """
        pp = pool.ProcessPool()
        self.assertEquals(pp.started, False)
        self.assertEquals(pp.finished, False)
        self.assertEquals(pp.processes, set())
        self.assertEquals(pp._finishCallbacks, {})
        
        def _checks(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            return pp.stop()
        
        def _closingUp(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, True)
            self.assertEquals(len(pp.processes), 0)
            self.assertEquals(pp._finishCallbacks, {})
        return pp.start().addCallback(_checks).addCallback(_closingUp)

    def test_adjustPoolSize(self):
        """
        Test that calls to pool.adjustPoolSize are correctly handled.
        """
        pp = pool.ProcessPool(min=10)
        self.assertEquals(pp.started, False)
        self.assertEquals(pp.finished, False)
        self.assertEquals(pp.processes, set())
        self.assertEquals(pp._finishCallbacks, {})
        
        def _resize1(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            return pp.adjustPoolSize(min=2, max=3)
        
        def _resize2(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(pp.max, 3)
            self.assertEquals(pp.min, 2)
            self.assertEquals(len(pp.processes), pp.max)
            self.assertEquals(len(pp._finishCallbacks), pp.max)
        
        def _resize3(_):
            return self.assertFailure(pp.adjustPoolSize(min=-1, max=5), AssertionError
                ).addCallback(lambda _:
                    self.assertFailure(pp.adjustPoolSize(min=5, max=1), AssertionError)
                ).addCallback(lambda _:
                    pp.stop()
                )
        
        return pp.start(
            ).addCallback(_resize1
            ).addCallback(_resize2
            ).addCallback(_resize3)

    def test_childRestart(self):
        """
        Test that a failing child process is immediately restarted.
        """
        pp = pool.ProcessPool(BadChild, min=1)
        STRING = "DATA"
        
        def _checks(_):
            d = pp._finishCallbacks.values()[0]
            pp.doWork(Die).addErrback(lambda _: None)
            return d.addBoth(_checksAgain)
        
        def _checksAgain(_):
            return pp.doWork(commands.Echo, data=STRING
                    ).addCallback(lambda result: self.assertEquals(result['response'], STRING))
        
        return pp.start(
            ).addCallback(_checks
            ).addCallback(lambda _: pp.stop())

    def test_parentProtocolChange(self):
        """
        Test that the father can use an AMP protocol too.
        """
        DATA = "CIAO"
        APPEND = "123"

        class Parent(amp.AMP):
            def pong(self, data):
                return {'response': DATA+APPEND}
            Pong.responder(pong)
        
        pp = pool.ProcessPool(Child, ampParent=Parent)
        def _checks(_):
            return pp.doWork(Ping, data=DATA
                       ).addCallback(lambda response:
                            self.assertEquals(response['response'], DATA+APPEND)
                       )

        return pp.start().addCallback(_checks).addCallback(lambda _: pp.stop())


    def test_deferToAMPProcess(self):
        """
        Test that deferToAMPProcess works as expected.
        """
        
        STRING = "CIAOOOO"
        return pool.deferToAMPProcess(commands.Echo, data=STRING
           ).addCallback(lambda result: self.assertEquals(result['response'], STRING)
           ).addCallback(lambda _: pool.pp.stop())

    def test_checkStateInPool(self):
        """
        Test that busy and ready lists are correctly maintained.
        """
        pp = pool.ProcessPool(WaitingChild)
        
        DATA = "foobar"

        def _checks(_):
            d = pp.doWork(First, data=DATA)
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            self.assertEquals(len(pp.ready), pp.min-1)
            self.assertEquals(len(pp.busy), 1)
            child = pp.busy.pop()
            pp.busy.add(child)
            child.callRemote(Second)
            return d

        return pp.start(
            ).addCallback(_checks
            ).addCallback(lambda _: pp.stop())

    def test_growingToMax(self):
        """
        Test that the pool grows over time until it reaches max processes.
        """
        MAX = 5
        pp = pool.ProcessPool(WaitingChild, min=1, max=MAX)

        def _checks(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            
            D = "DATA"
            d = [pp.doWork(First, data=D) for x in xrange(MAX)]

            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.max)
            self.assertEquals(len(pp._finishCallbacks), pp.max)
            
            [child.callRemote(Second) for child in pp.processes]
            return defer.DeferredList(d)

        return pp.start(
            ).addCallback(_checks
            ).addCallback(lambda _: pp.stop())
    
    def test_growingToMaxAndShrinking(self):
        """
        Test that the pool grows but after 'idle' time the number of
        processes goes back to the minimum.
        """
        
        MAX = 5
        MIN = 1
        IDLE = 1
        pp = pool.ProcessPool(WaitingChild, min=MIN, max=MAX, maxIdle=IDLE)
                
        def _checks(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            
            D = "DATA"
            d = [pp.doWork(First, data=D) for x in xrange(MAX)]

            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.max)
            self.assertEquals(len(pp._finishCallbacks), pp.max)
            
            [child.callRemote(Second) for child in pp.processes]
            return defer.DeferredList(d).addCallback(_realChecks)
            
        def _realChecks(_):
            from twisted.internet import reactor
            d = defer.Deferred()
            def _cb():
                def __(_):
                    try:
                        self.assertEquals(pp.started, True)
                        self.assertEquals(pp.finished, False)
                        self.assertEquals(len(pp.processes), pp.min)
                        self.assertEquals(len(pp._finishCallbacks), pp.min)
                        d.callback(None)
                    except Exception, e:
                        d.errback(e)
                return pp._pruneProcesses().addCallback(__)
            # just to be shure we are called after the pruner
            pp.looping.stop() # stop the looping, we don't want it to
                              # this right here
            reactor.callLater(IDLE, _cb)
            return d
        
        return pp.start(
            ).addCallback(_checks
            ).addCallback(lambda _: pp.stop())

    def test_recycling(self):
        """
        Test that after a given number of calls subprocesses are
        recycled.
        """
        MAX = 1
        MIN = 1
        RECYCLE_AFTER = 1
        pp = pool.ProcessPool(PidChild, min=MIN, max=MAX, recycleAfter=RECYCLE_AFTER)
        
        def _checks(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            return pp.doWork(Pid
                ).addCallback(lambda response: response['pid'])
        
        def _checks2(pid):
            return pp.doWork(Pid
                ).addCallback(lambda response: response['pid']
                ).addCallback(self.assertNotEquals, pid)
        
        def finish(reason):
            return pp.stop().addCallback(lambda _: reason)

        return pp.start(
            ).addCallback(_checks
            ).addCallback(_checks2
            ).addCallback(finish)
    
    def test_recyclingWithQueueOverload(self):
        """
        Test that we get the correct number of different results when
        we overload the pool of calls.
        """
        MAX = 5
        MIN = 1
        RECYCLE_AFTER = 10
        CALLS = 60
        pp = pool.ProcessPool(PidChild, min=MIN, max=MAX, recycleAfter=RECYCLE_AFTER)
        
        def _check(results):
            s = set()
            for succeed, response in results:
                s.add(response['pid'])
            self.assertEquals(len(s), MAX*math.ceil(float(CALLS)/(MAX*RECYCLE_AFTER)))
        
        def _work(_):
            l = [pp.doWork(Pid) for x in xrange(CALLS)]
            d = defer.DeferredList(l)
            return d.addCallback(_check)
        return pp.start(
            ).addCallback(_work
            ).addCallback(lambda _: pp.stop())

    def test_disableProcessRecycling(self):
        """
        Test that by setting 0 to recycleAfter we actually disable process recycling.
        """
        MAX = 1
        MIN = 1
        RECYCLE_AFTER = 0
        pp = pool.ProcessPool(PidChild, min=MIN, max=MAX, recycleAfter=RECYCLE_AFTER)
        
        def _checks(_):
            self.assertEquals(pp.started, True)
            self.assertEquals(pp.finished, False)
            self.assertEquals(len(pp.processes), pp.min)
            self.assertEquals(len(pp._finishCallbacks), pp.min)
            return pp.doWork(Pid
                ).addCallback(lambda response: response['pid'])
        
        def _checks2(pid):
            return pp.doWork(Pid
                ).addCallback(lambda response: response['pid']
                ).addCallback(self.assertEquals, pid
                ).addCallback(lambda _: pid)
        
        def finish(reason):
            return pp.stop().addCallback(lambda _: reason)

        return pp.start(
            ).addCallback(_checks
            ).addCallback(_checks2
            ).addCallback(_checks2
            ).addCallback(finish)        



