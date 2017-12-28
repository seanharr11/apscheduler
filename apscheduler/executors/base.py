from abc import ABCMeta, abstractmethod
from collections import defaultdict
from datetime import datetime
from traceback import format_tb
import logging
import sys

import six

from apscheduler.events import (
    JobExecutionEvent, EVENT_JOB_ERROR, EVENT_JOB_EXECUTED)


class MaxInstancesReachedError(Exception):
    def __init__(self, job):
        super(MaxInstancesReachedError, self).__init__(
            'Job "%s" has already reached its maximum number of instances (%d)' %
            (job.id, job.max_instances))


class BaseExecutor(six.with_metaclass(ABCMeta, object)):
    """Abstract base class that defines the interface that every executor must implement."""

    _scheduler = None
    _lock = None
    _logger = logging.getLogger('apscheduler.executors')

    def __init__(self):
        super(BaseExecutor, self).__init__()
        self._instances = defaultdict(lambda: 0)

    def start(self, scheduler, alias):
        """
        Called by the scheduler when the scheduler is being started or when the executor is being
        added to an already running scheduler.

        :param apscheduler.schedulers.base.BaseScheduler scheduler: the scheduler that is starting
            this executor
        :param str|unicode alias: alias of this executor as it was assigned to the scheduler

        """
        self._scheduler = scheduler
        self._lock = scheduler._create_lock()
        self._logger = logging.getLogger('apscheduler.executors.%s' % alias)

    def shutdown(self, wait=True):
        """
        Shuts down this executor.

        :param bool wait: ``True`` to wait until all submitted jobs
            have been executed
        """

    def submit_job(self, job, run_time):
        """
        Submits job for execution.

        :param Job job: job to execute
        :raises MaxInstancesReachedError: if the maximum number of
            allowed instances for this job has been reached

        """
        assert self._lock is not None, 'This executor has not been started yet'
        with self._lock:
            if self._instances[job.id] >= job.max_instances:
                raise MaxInstancesReachedError(job)
            job_submission_id = self._scheduler._add_job_submission(job)
            self._instances[job.id] += 1
            self._do_submit_job(job, job_submission_id, run_time)

    @abstractmethod
    def _do_submit_job(self, job, job_submission_id, run_time):
        """Performs the actual task of scheduling `run_job` to be called."""

    def _handle_job_event(self, event):
        
        self._logger.info("Handling event '{0}'".format(event))
        if event.code == EVENT_JOB_ERROR:
            self._run_job_error(event)
        elif event.code == EVENT_JOB_EXECUTED:
            self._run_job_success(event)

    def _run_job_success(self, event):
        """
        Called by the executor with the list of generated events when :func:`run_job` has been
        successfully called.

        """
        with self._lock:
            self._instances[event.job_id] -= 1
            if self._instances[event.job_id] == 0:
                del self._instances[event.job_id]

        now = datetime.now()
        self._scheduler._update_job_submission(event.job_submission_id, event.jobstore,
                                               completed_at=now, state='success')
        self._scheduler._dispatch_event(event)
        

    def _run_job_error(self, event):
        """Called by the executor with the exception if there is an error  calling `run_job`."""
        with self._lock:
            self._instances[event.job_id] -= 1
            if self._instances[event.job_id] == 0:
                del self._instances[event.job_id]

        now = datetime.now()
        
        exc_msg=event.exception
        
        self._scheduler._update_job_submission(event.job_submission_id, event.jobstore,
                state='failure', exc_msg=exc_msg, traceback=event.traceback, completed_at=now)
        self._logger.error('Handled error found when running job %s', event.job_id)
        self._scheduler._dispatch_event(event)


def run_job(job, logger_name, job_submission_id, jobstore_alias, run_time):
    """
    Called by executors to run the job. Returns a list of scheduler events to be dispatched by the
    scheduler.

    """
    event = None
    logger = logging.getLogger(logger_name)
    logger.info('Running job "%s" (scheduled at %s)', job, run_time)
    try:
        retval = job.func(*job.args, **job.kwargs)
    except:
        exc, tb = sys.exc_info()[1:]
        formatted_tb = ''.join(format_tb(tb))
        event = JobExecutionEvent(EVENT_JOB_ERROR, job.id, jobstore_alias, run_time,
                                        job_submission_id, exception=exc,
                                        traceback=formatted_tb)
        logger.exception('Job "%s" raised an exception', job)
        
    else:
        event = JobExecutionEvent(EVENT_JOB_EXECUTED, job.id, jobstore_alias, run_time,
                                        job_submission_id, retval=retval)
        logger.info('Job "%s" executed successfully', job)

    return event
