import datetime
import pytz
import time
from random import Random
from threading import Condition, Event, Thread

from crab import CrabError, CrabEvent, CrabStatus
from crab.util.schedule import CrabSchedule

HISTORY_COUNT = 10

class JobDeleted(Exception):
    """Exception raised by _initialize_job if the job can not be found."""
    pass

class CrabMonitor(Thread):
    """A class implementing the crab monitor thread."""

    def __init__(self, store):
        """Constructor.

        Saves the given storage backend and prepares the instance
        data."""
        Thread.__init__(self)

        self.store = store
        self.sched = {}
        self.status = {}
        self.status_ready = Event()
        self.config = {}
        self.last_start = {}
        self.timeout = {}
        self.miss_timeout = {}
        self.max_startid = 0
        self.max_warnid = 0
        self.max_finishid = 0
        self.last_time = None
        self.new_event = Condition()
        self.num_warning = 0
        self.num_error = 0
        self.random = Random()

    def run(self):
        """Monitor thread main run function.

        When the thread is started, this function will run.  It begins
        by fetching a list of jobs and using them to populate its
        data structures.  When this is complete, the Event status_ready
        is fired.

        It then goes into a loop, and every few seconds it checks
        for new events, processing any which are found.  The new_event
        Condition is fired if there were any new events.

        If the minute has changes snice the last time round the loop,
        the job scheduling is checked.  At this stage we also check
        for new / deleted / updated jobs."""

        jobs = self.store.get_jobs()

        for job in jobs:
            id_ = job['id']
            try:
                self._initialize_job(id_)

                # Allow a margin of events over HISTORY_COUNT to allow
                # for start events and warnings.
                events = self.store.get_job_events(id_, 4 * HISTORY_COUNT)

                # Events are returned newest-first but we need to work
                # through them in order.
                for event in reversed(events):
                    self._update_max_id_values(event)
                    self._process_event(id_, event)

                self._compute_reliability(id_)

            except JobDeleted:
                print 'Warning: job', id_, 'has vanished'

        self.status_ready.set()

        while True:
            time.sleep(5)
            datetime_ = datetime.datetime.now(pytz.UTC)

            events = self.store.get_events_since(self.max_startid,
                                self.max_warnid, self.max_finishid)
            for event in events:
                id_ = event['jobid']
                self._update_max_id_values(event)

                try:
                    if id_ not in self.status:
                        self._initialize_job(id_)

                    self._process_event(id_, event)
                    self._compute_reliability(id_)

                # If the monitor is loaded when a job has just been
                # deleted, then it may have events more recent
                # than those of the events that still exist.
                except JobDeleted:
                    pass

            self.num_error = 0;
            self.num_warning = 0;
            for id_ in self.status:
                jobstatus = self.status[id_]['status']
                if (jobstatus is None or CrabStatus.is_ok(jobstatus)):
                    pass
                elif (CrabStatus.is_warning(jobstatus)):
                    self.num_warning += 1;
                else:
                    self.num_error += 1;

            if events:
                with self.new_event:
                    self.new_event.notify_all()

            # Hour and minute should be sufficient to check
            # that the minute has changed.

            # TODO: check we didn't somehow miss a minute?

            time_stamp = datetime_.strftime('%H%M')

            if self.last_time is None or time_stamp != self.last_time:
                for id_ in self.sched:
                    if self.sched[id_].match(datetime_):
                        if ((id_ not in self.last_start) or
                                (self.last_start[id_] +
                                 self.config[id_]['graceperiod'] < datetime_)):
                            self._write_warning(id_, CrabStatus.LATE)
                            self.miss_timeout[id_] = (datetime_ +
                                    self.config[id_]['graceperiod'])

                # Look for new or deleted jobs.
                currentjobs = set(self.status.keys())
                jobs = self.store.get_jobs()
                for job in jobs:
                    id_ = job['id']
                    if id_ in currentjobs:
                        currentjobs.discard(id_)

                        # Compare installed timestamp is case we need to
                        # reload the schedule.  NOTE: assumes database
                        # datetimes compare in the correct order.
                        if job['installed'] > self.status[id_]['installed']:
                            self._schedule_job(id_)
                            self.status[id_]['installed'] = job['installed']
                    else:
                        # No need to check event history: if there were any
                        # events, we would have added the job when they
                        # occurred (unless a job was just un-deleted or the
                        # an event happend during the schedule check.
                        try:
                            self._initialize_job(id_)
                        except JobDeleted:
                            print 'Warning: job', id_, 'has vanished'

                # Remove (presumably deleted) jobs.
                for id_ in currentjobs:
                    self._remove_job(id_)

            self.last_time = time_stamp

            # Check status of timeouts - need to get a list of keys
            # so that we can delete from the dict while iterating.

            for id_ in self.miss_timeout.keys():
                if self.miss_timeout[id_] < datetime_:
                    self._write_warning(id_, CrabStatus.MISSED)
                    del self.miss_timeout[id_]

            for id_ in self.timeout.keys():
                if self.timeout[id_] < datetime_:
                    self._write_warning(id_, CrabStatus.TIMEOUT)
                    del self.timeout[id_]

    def _initialize_job(self, id_):
        """Fetches information about the specified job and records it
        in the instance data structures.  Includes a call to _schedule_job."""

        jobinfo = self.store.get_job_info(id_)
        if jobinfo is None or jobinfo['deleted'] is not None:
            raise JobDeleted

        self.status[id_] = {'status': None, 'running': False, 'history': [],
                            'installed': jobinfo['installed']}

        self._schedule_job(id_, jobinfo)

        # TODO: read these from the jobsettings table
        self.config[id_] = {'graceperiod': datetime.timedelta(minutes=2),
                            'timeout': datetime.timedelta(minutes=5)}

    def _schedule_job(self, id_, jobinfo=None):
        """Sets or updates job scheduling information.

        The job information can either be passed in as a dict, or it
        will be fetched from the storage backend.  If scheduling information
        (i.e. a "time" string, and optionally a timezone) is present,
        a CrabSchedule object is constructed and stored in the sched dict."""

        if jobinfo is None:
            jobinfo = self.store.get_job_info(id_)

        self.status[id_]['scheduled'] = False

        if jobinfo is not None and jobinfo['time'] is not None:
            try:
                self.sched[id_] = CrabSchedule(jobinfo['time'],
                                               jobinfo['timezone'])
            except CrabError as err:
                print 'Warning: could not add schedule: ' + str(err)

            else:
                self.status[id_]['scheduled'] = True

    def _remove_job(self, id_):
        """Removes a job from the instance data structures."""

        try:
            del self.status[id_]
            if id_ in self.config:
                del self.config[id_]
            if id_ in self.sched:
                del self.sched[id_]
            if id_ in self.last_start:
                del self.last_start[id_]
            if id_ in self.timeout:
                del self.timeout[id_]
            if id_ in self.miss_timeout:
                del self.miss_timeout[id_]
        except KeyError:
            print 'Warning: stopping monitoring job but it is not in monitor.'

    def _update_max_id_values(self, event):
        """Updates the instance max_startid, max_warnid and max_finishid
        values if they are outdate by the event, which is passed as a dict."""

        if (event['type'] == CrabEvent.START and
                event['id'] > self.max_startid):
            self.max_startid = event['id']
        if (event['type'] == CrabEvent.WARN and
                event['id'] > self.max_warnid):
            self.max_warnid = event['id']
        if (event['type'] == CrabEvent.FINISH and
                event['id'] > self.max_finishid):
            self.max_finishid = event['id']

    def _process_event(self, id_, event):
        """Processes the given event, updating the instance data
        structures accordingly."""

        datetime_ = self.store.parse_datetime(event['datetime'])

        if event['status'] is not None:
            status = event['status']
            prevstatus = self.status[id_]['status']

            # Avoid overwriting a status with a less important one.

            if CrabStatus.is_trivial(status):
                if prevstatus is None or CrabStatus.is_ok(prevstatus):
                    self.status[id_]['status'] = status

            elif CrabStatus.is_warning(status):
                if prevstatus is None or not CrabStatus.is_error(prevstatus):
                    self.status[id_]['status'] = status

            # Always set success / failure status (the remaining options).

            else:
                self.status[id_]['status'] = status

            if not CrabStatus.is_trivial(status):
                history = self.status[id_]['history']
                if len(history) >= HISTORY_COUNT:
                    del history[0]
                history.append(status)

        if event['type'] == CrabEvent.START:
            self.status[id_]['running'] = True
            self.last_start[id_] = datetime_
            self.timeout[id_] = datetime_ + self.config[id_]['timeout']
            if id_ in self.miss_timeout:
                del self.miss_timeout[id_]

        if (event['type'] == CrabEvent.FINISH
                or event['status'] == CrabStatus.TIMEOUT):
            self.status[id_]['running'] = False
            if id_ in self.timeout:
                del self.timeout[id_]

    def _compute_reliability(self, id_):
        """Uses the history list of the specified job to recalculate its
        reliability percentage and store it in the 'reliability'
        entry of the status dict."""

        history = self.status[id_]['history']
        if len(history) == 0:
            self.status[id_]['reliability'] = 0
        else:
            self.status[id_]['reliability'] = int(100 *
                len(filter(lambda x: x == CrabStatus.SUCCESS, history)) /
                len(history))

    def _write_warning(self, id_, status):
        """Inserts a warning into the storage backend."""
        try:
            self.store.log_warning(id_, status)
        except CrabError as err:
            print 'Could not record warning : ' + str(err)

    def get_job_status(self):
        """Fetches the status of all jobs as a dict.

        For efficiency this returns a reference to our job status dict.
        Callers should not modify it."""

        self.status_ready.wait()
        return self.status

    def wait_for_event_since(self, startid, warnid, finishid, timeout=120):
        """Function which waits for new events.

        It does this by comparing the IDs with our maximum values seen so
        far.  If no new events have already be seen, wait for the new_event
        Condition to fire.

        A random time up to 20s is added to the timeout to stagger requests."""

        if (self.max_startid > startid or self.max_warnid > warnid or
                                          self.max_finishid > finishid):
            pass
        else:
            with self.new_event:
                self.new_event.wait(timeout + self.random.randint(0,20))

        return {'startid': self.max_startid, 'warnid': self.max_warnid,
                'finishid': self.max_finishid, 'status': self.status,
                'numwarning': self.num_warning, 'numerror': self.num_error}