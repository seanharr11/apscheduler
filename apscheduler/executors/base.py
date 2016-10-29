from abc import ABCMeta, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta
from traceback import format_tb
import logging
import sys

from pytz import utc
import six

from apscheduler.events import (
    JobExecutionEvent, EVENT_JOB_MISSED, EVENT_JOB_ERROR, EVENT_JOB_EXECUTED)
try:
    from inspect import iscoroutinefunction
    from apscheduler.executors.base_py3 import generate_run_coroutine_job_closure
except ImportError:
    def iscoroutinefunction(func):
        return False


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
        self._instances = defaultdict(lambda: [])

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

    def submit_job(self, job, run_times):
        """
        Submits job for execution.

        :param Job job: job to execute
        :param list[datetime] run_times: list of datetimes specifying
            when the job should have been run
        :raises MaxInstancesReachedError: if the maximum number of
            allowed instances for this job has been reached

        """
        assert self._lock is not None, 'This executor has not been started yet'
        with self._lock:
            if len(self._instances[job.id]) >= job.max_instances:
                raise MaxInstancesReachedError(job)
            self._prepare_job_submission(job, run_times)

    def _prepare_job_submission(self, job, run_times):
        # Add the job instance to the jobstore
        job_submission_id = job._scheduler._jobstores[job._jobstore_alias].\
                add_job_submission(job)
        self._instances[job.id].append(job_submission_id)
        if iscoroutinefunction(job.func):
            self._do_submit_job(job,
                                run_times,
                                # Bind "job_submission_id" to "run_job()" in a closure
                                generate_run_coroutine_job_closure(job_submission_id))
        else:
            self._do_submit_job(job,
                                run_times,
                                # Build the same closure, only return a coroutine
                                generate_run_job_closure(job_submission_id))

    @abstractmethod
    def _do_submit_job(self, job, run_times, run_job_func):
        """
        Performs the actual task of scheduling `run_job_func` to be called.

        :param run_job_func func|coroutine: The function or coroutine to be executed

        """

    def _run_job_success(self, job_id, job_instance_id, events):
        """
        Called by the executor with the list of generated events when :func:`run_job` has been
        successfully called.

        """
        with self._lock:
            self._instances[job_id].remove(job_instance_id)
            if len(self._instances[job_id]) == 0:
                del self._instances[job_id]
        self.update_job_submission(job_instance_id, state="success")
        for event in events:
            self._scheduler._dispatch_event(event)

    def _run_job_error(self, job_id, exc, job_instance_id, traceback=None):
        """Called by the executor with the exception if there is an error  calling `run_job`."""
        with self._lock:
            self._instances[job_id].remove(job_instance_id)
            if len(self._instances[job_id]) == 0:
                del self._instances[job_id]
        self.update_job_submission(job_instance_id, state="failure")

        exc_info = (exc.__class__, exc, traceback)
        self._logger.error('Error running job %s', job_id, exc_info=exc_info)


def generate_run_job_closure(job_submission_id):
    """ Generate a closure so that "run_job" can see 'job_submission_id' later """
    def run_job(job, jobstore_alias, run_times, logger_name):
        """
        Called by executors to run the job. Returns a list of scheduler events to be
        dispatched by the scheduler.

        """
        events = []
        logger = logging.getLogger(logger_name)
        for run_time in run_times:
            # See if the job missed its run time window, and handle
            # possible misfires accordingly
            if job.misfire_grace_time is not None:
                difference = datetime.now(utc) - run_time
                grace_time = timedelta(seconds=job.misfire_grace_time)
                if difference > grace_time:
                    events.append(JobExecutionEvent(EVENT_JOB_MISSED, job.id, jobstore_alias,
                                                    run_time))
                    logger.warning('Run time of job "%s" was missed by %s', job, difference)
                    continue

            logger.info('Running job "%s" (scheduled at %s)', job, run_time)
            # Update the job submission to "running"
            job._scheduler._jobstores[jobstore_alias].\
                update_job_submission(job_submission_id, state="running")
            try:
                retval = job.func(*job.args, **job.kwargs)
            except:
                exc, tb = sys.exc_info()[1:]
                formatted_tb = ''.join(format_tb(tb))
                events.append(JobExecutionEvent(EVENT_JOB_ERROR, job.id, jobstore_alias, run_time,
                                                exception=exc, traceback=formatted_tb))
                logger.exception('Job "%s" raised an exception', job)
                job._scheduler._jobstores[jobstore_alias].\
                    update_job_submission(job_submission_id, state="failure")
            else:
                events.append(JobExecutionEvent(EVENT_JOB_EXECUTED, job.id, jobstore_alias,
                                                run_time,
                                                retval=retval))
                logger.info('Job "%s" executed successfully', job)
                job._scheduler._jobstores[jobstore_alias].\
                    update_job_submission(job_submission_id, state="success")

        return events
    return run_job
