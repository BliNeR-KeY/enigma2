from __future__ import division
from __future__ import print_function
from bisect import insort
from boxbranding import getMachineBrand, getMachineName
from datetime import datetime
from os import fsync, remove, rename, system
from os.path import exists
from time import ctime, time, strftime, localtime, mktime

from enigma import eActionMap, quitMainloop

import NavigationInstance
import six
from timer import Timer, TimerEntry
from Components.config import config
from Components.TimerSanityCheck import TimerSanityCheck
from Screens.MessageBox import MessageBox
import Screens.Standby
from Tools.Directories import SCOPE_CONFIG, fileExists, fileReadXML, resolveFilename
from Tools import Notifications
from Tools.XMLTools import stringToXML

MODULE_NAME = __name__.split(".")[-1]

#global variables begin
DSsave = False
RSsave = False
RBsave = False
aeDSsave = False
wasTimerWakeup = False
try:
	from Screens.InfoBar import InfoBar
except Exception as e:
	print("[PowerTimer] import from 'Screens.InfoBar import InfoBar' failed:", e)
	InfoBar = False
#+++
debug = False
#+++
#global variables end
#----------------------------------------------------------------------------------------------------
#Timer shutdown, reboot and restart priority
#1. wakeup
#2. wakeuptostandby						-> (same as 1.)
#3. deepstandby							->	DSsave
#4. deppstandby after event				->	aeDSsave
#5. reboot system						->	RBsave
#6. restart gui							->	RSsave
#7. standby
#8. autostandby
#9. nothing (no function, only for suppress autodeepstandby timer)
#10. autodeepstandby
#-overlapping timers or next timer start is within 15 minutes, will only the high-order timer executed (at same types will executed the next timer)
#-autodeepstandby timer is only effective if no other timer is active or current time is in the time window
#-priority for repeated timer: shift from begin and end time only temporary, end-action priority is higher as the begin-action
#----------------------------------------------------------------------------------------------------

#reset wakeup state after ending timer


def resetTimerWakeup():
	global wasTimerWakeup
	if exists("/tmp/was_powertimer_wakeup"):
		remove("/tmp/was_powertimer_wakeup")
		if debug:
			print("[POWERTIMER] reset wakeup state")
	wasTimerWakeup = False

# Parses an event, and gives out a (begin, end)-tuple.
#
def parseEvent(event):
	begin = event.getBeginTime()
	end = begin + event.getDuration()
	return (begin, end)


class AFTEREVENT:
	def __init__(self):
		pass

	NONE = 0
	WAKEUP = 1
	WAKEUPTOSTANDBY = 2
	STANDBY = 3
	DEEPSTANDBY = 4


class TIMERTYPE:
	def __init__(self):
		pass

	NONE = 0
	WAKEUP = 1
	WAKEUPTOSTANDBY = 2
	AUTOSTANDBY = 3
	AUTODEEPSTANDBY = 4
	STANDBY = 5
	DEEPSTANDBY = 6
	REBOOT = 7
	RESTART = 8


class PowerTimer(Timer):
	def __init__(self):
		Timer.__init__(self)
		self.timersFilename = resolveFilename(SCOPE_CONFIG, "pm_timers.xml")
		self.loadTimers()

	def loadTimers(self):
		timersDom = fileReadXML(self.timersFilename, source=MODULE_NAME)
		if timersDom is None:
			if not exists(self.timersFilename):
				return
			Notifications.AddPopup(_("The timer file (pm_timers.xml) is corrupt and could not be loaded."), type=MessageBox.TYPE_ERROR, timeout=0, id="TimerLoadFailed")
			print("[PowerTimer] Error: Loading 'pm_timers.xml' failed!")
			try:
				rename(self.timersFilename, "%s_old" % self.timersFilename)
			except (IOError, OSError) as err:
				print("[PowerTimer] Error %d: Renaming broken timer file failed!  (%s)" % (err.errno, err.strerror))
			return
		# put out a message when at least one timer overlaps
		check = True
		for timer in timersDom.findall("timer"):
			newTimer = self.createTimer(timer)
			if (self.record(newTimer, True, dosave=False) is not None) and (check == True):
				Notifications.AddPopup(_("Timer overlap in pm_timers.xml detected!\nPlease recheck it!"), type=MessageBox.TYPE_ERROR, timeout=0, id="TimerLoadFailed")
				check = False # at moment it is enough when the message is displayed one time

	def loadTimer(self):
		return self.loadTimers()

	def saveTimers(self):
		savedays = 3600 * 24 * 7	#logs older 7 Days will not saved
		timerList = ["<?xml version=\"1.0\" ?>", "<timers>"]
		for timer in self.timer_list + self.processed_timers:
			if timer.dontSave:
				continue
			timerEntry = []
			timerEntry.append("timertype=\"%s\"" % stringToXML({
				TIMERTYPE.NONE: "nothing",
				TIMERTYPE.WAKEUP: "wakeup",
				TIMERTYPE.WAKEUPTOSTANDBY: "wakeuptostandby",
				TIMERTYPE.AUTOSTANDBY: "autostandby",
				TIMERTYPE.AUTODEEPSTANDBY: "autodeepstandby",
				TIMERTYPE.STANDBY: "standby",
				TIMERTYPE.DEEPSTANDBY: "deepstandby",
				TIMERTYPE.REBOOT: "reboot",
				TIMERTYPE.RESTART: "restart"
			}[timer.timerType]))
			timerEntry.append("begin=\"%d\"" % timer.begin)
			timerEntry.append("end=\"%d\"" % timer.end)
			timerEntry.append("repeated=\"%d\"" % timer.repeated)
			timerEntry.append("afterevent=\"%s\"" % stringToXML({
				AFTEREVENT.NONE: "nothing",
				AFTEREVENT.WAKEUP: "wakeup",
				AFTEREVENT.WAKEUPTOSTANDBY: "wakeuptostandby",
				AFTEREVENT.STANDBY: "standby",
				AFTEREVENT.DEEPSTANDBY: "deepstandby"
				}[timer.afterEvent]))
			timerEntry.append("disabled=\"%d\"" % timer.disabled)
			timerEntry.append("autosleepinstandbyonly=\"%s\"" % timer.autosleepinstandbyonly)
			timerEntry.append("autosleepdelay=\"%s\"" % timer.autosleepdelay)
			timerEntry.append("autosleeprepeat=\"%s\"" % timer.autosleeprepeat)
			timerEntry.append("autosleepwindow=\"%s\"" % timer.autosleepwindow)
			timerEntry.append("autosleepbegin=\"%d\"" % int(timer.autosleepbegin))
			timerEntry.append("autosleepend=\"%d\"" % int(timer.autosleepend))
			timerEntry.append("nettraffic=\"%s\"" % timer.nettraffic)
			timerEntry.append("trafficlimit=\"%s\"" % timer.trafficlimit)
			timerEntry.append("netip=\"%s\"" % timer.netip)
			timerEntry.append("ipadress=\"%s\"" % timer.ipadress)
			timerList.append("\t<timer %s>" % " ".join(timerEntry))

			for logTime, logCode, logMsg in timer.log_entries:
				if logTime > time() - savedays:
					timerList.append("\t\t<log code=\"%d\" time=\"%d\">%s</log>" % (logCode, logTime, stringToXML(logMsg)))

			timerList.append("\t</timer>")
		timerList.append("</timers>\n")
		# Should this code also use a writeLock as for the regular timers?
		file = open("%s.writing" % self.timersFilename, "w")
		file.write("\n".join(timerList))
		file.flush()
		fsync(file.fileno())
		file.close()
		rename("%s.writing" % self.timersFilename, self.timersFilename)

	def saveTimer(self):
		return self.saveTimers()

	def createTimer(self, timerDom):
		begin = int(timerDom.get("begin"))
		end = int(timerDom.get("end"))
		disabled = int(timerDom.get("disabled") or "0")
		afterevent = {
			"nothing": AFTEREVENT.NONE,
			"wakeup": AFTEREVENT.WAKEUP,
			"wakeuptostandby": AFTEREVENT.WAKEUPTOSTANDBY,
			"standby": AFTEREVENT.STANDBY,
			"deepstandby": AFTEREVENT.DEEPSTANDBY
		}.get(timerDom.get("afterevent", "nothing"), "nothing")
		timertype = {
			"nothing": TIMERTYPE.NONE,
			"wakeup": TIMERTYPE.WAKEUP,
			"wakeuptostandby": TIMERTYPE.WAKEUPTOSTANDBY,
			"autostandby": TIMERTYPE.AUTOSTANDBY,
			"autodeepstandby": TIMERTYPE.AUTODEEPSTANDBY,
			"standby": TIMERTYPE.STANDBY,
			"deepstandby": TIMERTYPE.DEEPSTANDBY,
			"reboot": TIMERTYPE.REBOOT,
			"restart": TIMERTYPE.RESTART
		}.get(timerDom.get("timertype", "wakeup"), "wakeup")
		repeated = six.ensure_str(timerDom.get("repeated"))
		autosleepbegin = int(timerDom.get("autosleepbegin") or begin)
		autosleepend = int(timerDom.get("autosleepend") or end)

		entry = PowerTimerEntry(begin, end, disabled, afterevent, timertype)
		entry.repeated = int(repeated)
		entry.autosleepinstandbyonly = timerDom.get("autosleepinstandbyonly", "no")
		entry.autosleepdelay = int(timerDom.get("autosleepdelay", "0"))
		entry.autosleeprepeat = timerDom.get("autosleeprepeat", "once")
		entry.autosleepwindow = timerDom.get("autosleepwindow", "no")
		entry.autosleepbegin = autosleepbegin
		entry.autosleepend = autosleepend

		entry.nettraffic = timerDom.get("nettraffic", "no")
		entry.trafficlimit = int(timerDom.get("trafficlimit", "100"))
		entry.netip = timerDom.get("netip", "no")
		entry.ipadress = timerDom.get("ipadress", "0.0.0.0")

		for log in timerDom.findall("log"):
			msg = six.ensure_str(log.text).strip()
			entry.log_entries.append((int(log.get("time")), int(log.get("code")), msg))

		return entry

	def doActivate(self, w):
		# When activating a timer which has already passed, simply
		# abort the timer.  Don't run trough all the stages.
		if w.shouldSkip():
			w.state = PowerTimerEntry.StateEnded
		else:
			# When active returns true, this means "accepted".
			# Otherwise, the current state is kept.
			# The timer entry itself will fix up the delay.
			if w.activate():
				w.state += 1
		try:
			self.timer_list.remove(w)
		except Exception:
			print("[PowerTimer] Remove list failed!")
		if w.state < PowerTimerEntry.StateEnded:  # Did this timer reached the last state?
			insort(self.timer_list, w)  # No, sort it into active list.
		else:  # Yes, process repeated, and re-add.
			if w.repeated:
				w.processRepeated()
				w.state = PowerTimerEntry.StateWaiting
				self.addTimerEntry(w)
			else:
				# Remove old timers as set in config.
				self.cleanupDaily(config.recording.keep_timers.value)  # DEBUG: This method does not appear to be defined!!!
				insort(self.processed_timers, w)
		self.stateChanged(w)


	def isAutoDeepstandbyEnabled(self):
		ret = True
		if Screens.Standby.inStandby:
			now = time()
			for timer in self.timer_list:
				if timer.timerType == TIMERTYPE.AUTODEEPSTANDBY:
					if timer.begin <= now + 900:
						ret = not (timer.getNetworkTraffic() or timer.getNetworkAdress())
					elif timer.autosleepwindow == 'yes':
						ret = timer.autosleepbegin <= now + 900
				if not ret:
					break
		return ret

	def isProcessing(self, exceptTimer=None, endedTimer=None):
		isRunning = False
		for timer in self.timer_list:
			if timer.timerType != TIMERTYPE.AUTOSTANDBY and timer.timerType != TIMERTYPE.AUTODEEPSTANDBY and timer.timerType != exceptTimer and timer.timerType != endedTimer:
				if timer.isRunning():
					isRunning = True
					break
		return isRunning

	def getNextZapTime(self):
		now = time()
		for timer in self.timer_list:
			if timer.begin < now:
				continue
			return timer.begin
		return -1

	def getNextPowerManagerTimeOld(self, getNextStbPowerOn=False):
		now = int(time())
		nextPTlist = [(-1, None, None, None)]
		for timer in self.timer_list:
			if timer.timerType != TIMERTYPE.AUTOSTANDBY and timer.timerType != TIMERTYPE.AUTODEEPSTANDBY:
				next_act = timer.getNextWakeup(getNextStbPowerOn)
				if next_act + 3 < now:
					continue
				if getNextStbPowerOn and debug:
					print("[Powertimer] next stb power up", strftime("%a, %Y/%m/%d %H:%M", localtime(next_act)))
				next_timertype = next_afterevent = None
				if nextPTlist[0][0] == -1:
					if abs(next_act - timer.begin) <= 30:
						next_timertype = timer.timerType
					elif abs(next_act - timer.end) <= 30:
						next_afterevent = timer.afterEvent
					nextPTlist = [(next_act, next_timertype, next_afterevent, timer.state)]
				else:
					if abs(next_act - timer.begin) <= 30:
						next_timertype = timer.timerType
					elif abs(next_act - timer.end) <= 30:
						next_afterevent = timer.afterEvent
					nextPTlist.append((next_act, next_timertype, next_afterevent, timer.state))
		nextPTlist.sort()
		return nextPTlist

	def getNextPowerManagerTime(self, getNextStbPowerOn=False, getNextTimerTyp=False):
		#getNextStbPowerOn = True returns tuple -> (timer.begin, set standby)
		#getNextTimerTyp = True returns next timer list -> [(timer.begin, timer.timerType, timer.afterEvent, timer.state)]
		global DSsave, RSsave, RBsave, aeDSsave
		nextrectime = self.getNextPowerManagerTimeOld(getNextStbPowerOn)
		faketime = int(time()) + 300

		if getNextStbPowerOn:
			if config.timeshift.isRecording.value:
				if 0 < nextrectime[0][0] < faketime:
					return nextrectime[0][0], int(nextrectime[0][1] == 2 or nextrectime[0][2] == 2)
				else:
					return faketime, 0
			else:
				return nextrectime[0][0], int(nextrectime[0][1] == 2 or nextrectime[0][2] == 2)
		elif getNextTimerTyp:
			#check entrys and plausibility of shift state (manual canceled timer has shift/save state not reset)
			tt = ae = []
			now = time()
			if debug:
				print("+++++++++++++++")
			for entry in nextrectime:
				if entry[0] < now + 900:
					tt.append(entry[1])
				if entry[0] < now + 900:
					ae.append(entry[2])
				if debug:
					print(ctime(entry[0]), entry)
			if not TIMERTYPE.RESTART in tt:
				RSsave = False
			if not TIMERTYPE.REBOOT in tt:
				RBsave = False
			if not TIMERTYPE.DEEPSTANDBY in tt:
				DSsave = False
			if not AFTEREVENT.DEEPSTANDBY in ae:
				aeDSsave = False
			if debug:
				print("RSsave=%s, RBsave=%s, DSsave=%s, aeDSsave=%s, wasTimerWakeup=%s" % (RSsave, RBsave, DSsave, aeDSsave, wasTimerWakeup))
			if debug:
				print("+++++++++++++++")
			###
			if config.timeshift.isRecording.value:
				if 0 < nextrectime[0][0] < faketime:
					return nextrectime
				else:
					nextrectime.append((faketime, None, None, None))
					nextrectime.sort()
					return nextrectime
			else:
				return nextrectime
		else:
			if config.timeshift.isRecording.value:
				if 0 < nextrectime[0][0] < faketime:
					return nextrectime[0][0]
				else:
					return faketime
			else:
				return nextrectime[0][0]

	def isNextPowerManagerAfterEventActionAuto(self):
		for timer in self.timer_list:
			if timer.timerType == TIMERTYPE.WAKEUPTOSTANDBY or timer.afterEvent == AFTEREVENT.WAKEUPTOSTANDBY or timer.timerType == TIMERTYPE.WAKEUP or timer.afterEvent == AFTEREVENT.WAKEUP:
				return True
		return False

	def record(self, entry, ignoreTSC=False, dosave=True):		#wird von loadTimer mit dosave=False aufgerufen
		entry.timeChanged()
		print("[PowerTimer] Entry '%s'." % str(entry))
		entry.Timer = self
		self.addTimerEntry(entry)
		if dosave:
			self.saveTimers()
		return None

	def removeEntry(self, entry):
		print("[PowerTimer] Remove entry '%s'." % str(entry))
		entry.repeated = False  # Avoid re-enqueuing.
		entry.autoincrease = False
		entry.abort()  # Abort timer.  This sets the end time to current time, so timer will be stopped.
		if entry.state != entry.StateEnded:
			self.timeChanged(entry)
		# print("[PowerTimer] State: %s." % entry.state)
		# print("[PowerTimer] In processed: %s." % entry in self.processed_timers)
		# print("[PowerTimer] In running: %s." % entry in self.timer_list)
		if entry.state != 3:  # Disable timer first.
			entry.disable()
		if not entry.dontSave:  # Auto increase instant timer if possible.
			for timer in self.timer_list:
				if timer.setAutoincreaseEnd():
					self.timeChanged(timer)
		if entry in self.processed_timers:  # Now the timer should be in the processed_timers list, remove it from there.
			self.processed_timers.remove(entry)
		self.saveTimers()

	def shutdown(self):
		self.saveTimers()

	def cleanup(self):
		Timer.cleanup(self)
		self.saveTimers()

	def cleanupDaily(self, days):
		Timer.cleanupDaily(self, days)
		self.saveTimers()


class PowerTimerEntry(TimerEntry, object):
	def __init__(self, begin, end, disabled=False, afterEvent=AFTEREVENT.NONE, timerType=TIMERTYPE.WAKEUP, checkOldTimers=False, autosleepdelay=60):
		TimerEntry.__init__(self, int(begin), int(end))
		if checkOldTimers:
			if self.begin < time() - 1209600:
				self.begin = int(time())

		#check autopowertimer
		if (timerType == TIMERTYPE.AUTOSTANDBY or timerType == TIMERTYPE.AUTODEEPSTANDBY) and not disabled and time() > 3600 and self.begin > time():
			self.begin = int(time())						#the begin is in the future -> set to current time = no start delay of this timer

		if self.end < self.begin:
			self.end = self.begin

		self.dontSave = False
		self.disabled = disabled
		self.timer = None
		self.__record_service = None
		self.start_prepare = 0
		self.timerType = timerType
		self.afterEvent = afterEvent
		self.autoincrease = False
		self.autoincreasetime = 3600 * 24  # 1 day.
		self.autosleepinstandbyonly = "no"
		self.autosleepdelay = autosleepdelay
		self.autosleeprepeat = "once"
		self.autosleepwindow = "no"
		self.autosleepbegin = self.begin
		self.autosleepend = self.end

		self.nettraffic = "no"
		self.trafficlimit = 100
		self.netip = "no"
		self.ipadress = "0.0.0.0"

		self.log_entries = []
		self.resetState()

		self.messageBoxAnswerPending = False

	def __repr__(self, getType=False):
		timertype = {
			TIMERTYPE.NONE: "nothing",
			TIMERTYPE.WAKEUP: "wakeup",
			TIMERTYPE.WAKEUPTOSTANDBY: "wakeuptostandby",
			TIMERTYPE.AUTOSTANDBY: "autostandby",
			TIMERTYPE.AUTODEEPSTANDBY: "autodeepstandby",
			TIMERTYPE.STANDBY: "standby",
			TIMERTYPE.DEEPSTANDBY: "deepstandby",
			TIMERTYPE.REBOOT: "reboot",
			TIMERTYPE.RESTART: "restart"
			}[self.timerType]
		if getType:
			return timertype
		if not self.disabled:
			return "PowerTimerEntry(type=%s, begin=%s)" % (timertype, ctime(self.begin))
		else:
			return "PowerTimerEntry(type=%s, begin=%s Disabled)" % (timertype, ctime(self.begin))

	def log(self, code, msg):
		self.log_entries.append((int(time()), code, msg))

	def do_backoff(self):
		if Screens.Standby.inStandby and not wasTimerWakeup or RSsave or RBsave or aeDSsave or DSsave:
			self.backoff = 300
		else:
			if self.backoff == 0:
				self.backoff = 300
			else:
				self.backoff += 300
				if self.backoff > 900:
					self.backoff = 900
		self.log(10, "backoff: retry in %d minutes" % (int(self.backoff) / 60))

	def activate(self):
		global RSsave, RBsave, DSsave, aeDSsave, wasTimerWakeup, InfoBar

		if not InfoBar:
			try:
				from Screens.InfoBar import InfoBar
			except Exception as e:
				print("[PowerTimer] import from 'Screens.InfoBar import InfoBar' failed:", e)

		isRecTimerWakeup = breakPT = shiftPT = False
		now = time()
		next_state = self.state + 1
		self.log(5, "Activating state %d." % next_state)
		if next_state == self.StatePrepared and (self.timerType == TIMERTYPE.AUTOSTANDBY or self.timerType == TIMERTYPE.AUTODEEPSTANDBY):
			eActionMap.getInstance().bindAction('', -0x7FFFFFFF, self.keyPressed)
			if self.autosleepwindow == 'yes':
				ltm = localtime(now)
				asb = strftime("%H:%M", localtime(self.autosleepbegin)).split(':')
				ase = strftime("%H:%M", localtime(self.autosleepend)).split(':')
				self.autosleepbegin = int(mktime(datetime(ltm.tm_year, ltm.tm_mon, ltm.tm_mday, int(asb[0]), int(asb[1])).timetuple()))
				self.autosleepend = int(mktime(datetime(ltm.tm_year, ltm.tm_mon, ltm.tm_mday, int(ase[0]), int(ase[1])).timetuple()))
				if self.autosleepend <= self.autosleepbegin:
					self.autosleepbegin -= 86400
			if self.getAutoSleepWindow():
				if now < self.autosleepbegin and now > self.autosleepbegin - self.prepare_time - 3:	#begin is in prepare time window
					self.begin = self.end = self.autosleepbegin + int(self.autosleepdelay) * 60
				else:
					self.begin = self.end = int(now) + int(self.autosleepdelay) * 60
			else:
				return False
			if self.timerType == TIMERTYPE.AUTODEEPSTANDBY:
				self.getNetworkTraffic(getInitialValue=True)

		if next_state == self.StateRunning or next_state == self.StateEnded:
			if NavigationInstance.instance.PowerTimer is None:
				#TODO: running/ended timer at system start has no nav instance
				#First fix: crash in getPriorityCheck (NavigationInstance.instance.PowerTimer...)
				#Second fix: suppress the message (A finished powertimer wants to ...)
				if debug:
					print("*****NavigationInstance.instance.PowerTimer is None*****", self.timerType, self.state, ctime(self.begin), ctime(self.end))
				return True
			elif (next_state == self.StateRunning and abs(self.begin - now) > 900) or (next_state == self.StateEnded and abs(self.end - now) > 900):
				if self.timerType == TIMERTYPE.AUTODEEPSTANDBY or self.timerType == TIMERTYPE.AUTOSTANDBY:
					print('[Powertimer] time warp detected - set new begin time for %s timer' % self.__repr__(True))
					if not self.getAutoSleepWindow():
						return False
					else:
						self.begin = self.end = int(now) + int(self.autosleepdelay) * 60
						return False
				print('[Powertimer] time warp detected - timer %s ending without action' % self.__repr__(True))
				return True

			if NavigationInstance.instance.isRecordTimerImageStandard:
				isRecTimerWakeup = NavigationInstance.instance.RecordTimer.isRecTimerWakeup()
			if isRecTimerWakeup:
				wasTimerWakeup = True
			elif exists("/tmp/was_powertimer_wakeup") and not wasTimerWakeup:
				wasTimerWakeup = int(open("/tmp/was_powertimer_wakeup", "r").read()) and True or False

		if next_state == self.StatePrepared:
			self.log(6, "Prepare ok, waiting for begin: %s" % ctime(self.begin))
			self.backoff = 0
			return True

		elif next_state == self.StateRunning:

			# if this timer has been cancelled, just go to "end" state.
			if self.cancelled:
				return True

			if self.failed:
				return True

			if self.timerType == TIMERTYPE.NONE:
				return True

			elif self.timerType == TIMERTYPE.WAKEUP:
				if debug:
					print("self.timerType == TIMERTYPE.WAKEUP:")
				Screens.Standby.TVinStandby.skipHdmiCecNow('wakeuppowertimer')
				if Screens.Standby.inStandby:
					Screens.Standby.inStandby.Power()
				return True

			elif self.timerType == TIMERTYPE.WAKEUPTOSTANDBY:
				if debug:
					print("self.timerType == TIMERTYPE.WAKEUPTOSTANDBY:")
				return True

			elif self.timerType == TIMERTYPE.STANDBY:
				if debug:
					print("self.timerType == TIMERTYPE.STANDBY:")
				prioPT = [TIMERTYPE.WAKEUP, TIMERTYPE.RESTART, TIMERTYPE.REBOOT, TIMERTYPE.DEEPSTANDBY]
				prioPTae = [AFTEREVENT.WAKEUP, AFTEREVENT.DEEPSTANDBY]
				shiftPT, breakPT = self.getPriorityCheck(prioPT, prioPTae)
				if not Screens.Standby.inStandby and not breakPT: # not already in standby
					callback = self.sendStandbyNotification
					message = _("A finished powertimer wants to set your\n%s %s to standby. Do that now?") % (getMachineBrand(), getMachineName())
					messageboxtyp = MessageBox.TYPE_YESNO
					timeout = int(config.usage.shutdown_msgbox_timeout.value)
					default = True
					if InfoBar and InfoBar.instance:
						InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
					else:
						Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
				return True

			elif self.timerType == TIMERTYPE.AUTOSTANDBY:
				if debug:
					print("self.timerType == TIMERTYPE.AUTOSTANDBY:")
				if not self.getAutoSleepWindow():
					return False
				if not Screens.Standby.inStandby and not self.messageBoxAnswerPending: # not already in standby
					self.messageBoxAnswerPending = True
					callback = self.sendStandbyNotification
					message = _("A finished powertimer wants to set your\n%s %s to standby. Do that now?") % (getMachineBrand(), getMachineName())
					messageboxtyp = MessageBox.TYPE_YESNO
					timeout = int(config.usage.shutdown_msgbox_timeout.value)
					default = True
					if InfoBar and InfoBar.instance:
						InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
					else:
						Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
					if self.autosleeprepeat == "once":
						eActionMap.getInstance().unbindAction('', self.keyPressed)
						return True
					else:
						self.begin = self.end = int(now) + int(self.autosleepdelay) * 60
				else:
					self.begin = self.end = int(now) + int(self.autosleepdelay) * 60

			elif self.timerType == TIMERTYPE.AUTODEEPSTANDBY:
				if debug:
					print("self.timerType == TIMERTYPE.AUTODEEPSTANDBY:")
				if not self.getAutoSleepWindow():
					return False
				if isRecTimerWakeup or (self.autosleepinstandbyonly == 'yes' and not Screens.Standby.inStandby) \
					or NavigationInstance.instance.PowerTimer.isProcessing() or abs(NavigationInstance.instance.PowerTimer.getNextPowerManagerTime() - now) <= 900 or self.getNetworkAdress() or self.getNetworkTraffic() \
					or NavigationInstance.instance.RecordTimer.isRecording() or abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - now) <= 900 or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - now) <= 900:
					self.do_backoff()
					# retry
					self.begin = self.end = int(now) + self.backoff
					return False
				elif not Screens.Standby.inTryQuitMainloop: # not a shutdown messagebox is open
					if self.autosleeprepeat == "once":
						self.disabled = True
					if Screens.Standby.inStandby or self.autosleepinstandbyonly == 'noquery': # in standby or option 'without query' is enabled
						print("[PowerTimer] quitMainloop #1")
						quitMainloop(1)
						return True
					elif not self.messageBoxAnswerPending:
						self.messageBoxAnswerPending = True
						callback = self.sendTryQuitMainloopNotification
						message = _("A finished powertimer wants to shutdown your %s %s.\nDo that now?") % (getMachineBrand(), getMachineName())
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = int(config.usage.shutdown_msgbox_timeout.value)
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
						if self.autosleeprepeat == "once":
							eActionMap.getInstance().unbindAction('', self.keyPressed)
							return True
					self.begin = self.end = int(now) + int(self.autosleepdelay) * 60

			elif self.timerType == TIMERTYPE.RESTART:
				if debug:
					print("self.timerType == TIMERTYPE.RESTART:")
				#check priority
				prioPT = [TIMERTYPE.RESTART, TIMERTYPE.REBOOT, TIMERTYPE.DEEPSTANDBY]
				prioPTae = [AFTEREVENT.DEEPSTANDBY]
				shiftPT, breakPT = self.getPriorityCheck(prioPT, prioPTae)
				#a timer with higher priority was shifted - no execution of current timer
				if RBsave or aeDSsave or DSsave:
					if debug:
						print("break#1")
					breakPT = True
				#a timer with lower priority was shifted - shift now current timer and wait for restore the saved time values from other timer
				if False:
					if debug:
						print("shift#1")
					breakPT = False
					shiftPT = True
				#shift or break
				if isRecTimerWakeup or shiftPT or breakPT \
					or NavigationInstance.instance.RecordTimer.isRecording() or abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - now) <= 900 or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - now) <= 900:
					if self.repeated and not RSsave:
						self.savebegin = self.begin
						self.saveend = self.end
						RSsave = True
					if not breakPT:
						self.do_backoff()
						#check difference begin to end before shift begin time
						if RSsave and self.end - self.begin > 3 and self.end - now - self.backoff <= 240:
							breakPT = True
					#breakPT
					if breakPT:
						if self.repeated and RSsave:
							try:
								self.begin = self.savebegin
								self.end = self.saveend
							except:
								pass
						RSsave = False
						return True
					# retry
					oldbegin = self.begin
					self.begin = int(now) + self.backoff
					if abs(self.end - oldbegin) <= 3:
						self.end = self.begin
					else:
						if not self.repeated and self.end < self.begin + 300:
							self.end = self.begin + 300
					return False
				elif not Screens.Standby.inTryQuitMainloop: # not a shutdown messagebox is open
					if self.repeated and RSsave:
						try:
							self.begin = self.savebegin
							self.end = self.saveend
						except:
							pass
					if Screens.Standby.inStandby: # in standby
						print("[PowerTimer] quitMainloop #4")
						quitMainloop(3)
					else:
						callback = self.sendTryToRestartNotification
						message = _("A finished powertimer wants to restart the user interface.\nDo that now?")
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = int(config.usage.shutdown_msgbox_timeout.value)
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
				RSsave = False
				return True

			elif self.timerType == TIMERTYPE.REBOOT:
				if debug:
					print("self.timerType == TIMERTYPE.REBOOT:")
				#check priority
				prioPT = [TIMERTYPE.REBOOT, TIMERTYPE.DEEPSTANDBY]
				prioPTae = [AFTEREVENT.DEEPSTANDBY]
				shiftPT, breakPT = self.getPriorityCheck(prioPT, prioPTae)
				#a timer with higher priority was shifted - no execution of current timer
				if aeDSsave or DSsave:
					if debug:
						print("break#1")
					breakPT = True
				#a timer with lower priority was shifted - shift now current timer and wait for restore the saved time values from other timer
				if RSsave:
					if debug:
						print("shift#1")
					breakPT = False
					shiftPT = True
				#shift or break
				if isRecTimerWakeup or shiftPT or breakPT \
					or NavigationInstance.instance.RecordTimer.isRecording() or abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - now) <= 900 or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - now) <= 900:
					if self.repeated and not RBsave:
						self.savebegin = self.begin
						self.saveend = self.end
						RBsave = True
					if not breakPT:
						self.do_backoff()
						#check difference begin to end before shift begin time
						if RBsave and self.end - self.begin > 3 and self.end - now - self.backoff <= 240:
							breakPT = True
					#breakPT
					if breakPT:
						if self.repeated and RBsave:
							try:
								self.begin = self.savebegin
								self.end = self.saveend
							except:
								pass
						RBsave = False
						return True
					# retry
					oldbegin = self.begin
					self.begin = int(now) + self.backoff
					if abs(self.end - oldbegin) <= 3:
						self.end = self.begin
					else:
						if not self.repeated and self.end < self.begin + 300:
							self.end = self.begin + 300
					return False
				elif not Screens.Standby.inTryQuitMainloop: # not a shutdown messagebox is open
					if self.repeated and RBsave:
						try:
							self.begin = self.savebegin
							self.end = self.saveend
						except:
							pass
					if Screens.Standby.inStandby: # in standby
						print("[PowerTimer] quitMainloop #3")
						quitMainloop(2)
					else:
						callback = self.sendTryToRebootNotification
						message = _("A finished powertimer wants to reboot your %s %s.\nDo that now?") % (getMachineBrand(), getMachineName())
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = int(config.usage.shutdown_msgbox_timeout.value)
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
				RBsave = False
				return True

			elif self.timerType == TIMERTYPE.DEEPSTANDBY:
				if debug:
					print("self.timerType == TIMERTYPE.DEEPSTANDBY:")
				#check priority
				prioPT = [TIMERTYPE.WAKEUP, TIMERTYPE.WAKEUPTOSTANDBY, TIMERTYPE.DEEPSTANDBY]
				prioPTae = [AFTEREVENT.WAKEUP, AFTEREVENT.WAKEUPTOSTANDBY, AFTEREVENT.DEEPSTANDBY]
				shiftPT, breakPT = self.getPriorityCheck(prioPT, prioPTae)
				#a timer with higher priority was shifted - no execution of current timer
				if False:
					if debug:
						print("break#1")
					breakPT = True
				#a timer with lower priority was shifted - shift now current timer and wait for restore the saved time values from other timer
				if RSsave or RBsave or aeDSsave:
					if debug:
						print("shift#1")
					breakPT = False
					shiftPT = True
				#shift or break
				if isRecTimerWakeup or shiftPT or breakPT \
					or NavigationInstance.instance.RecordTimer.isRecording() or abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - now) <= 900 or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - now) <= 900:
					if self.repeated and not DSsave:
						self.savebegin = self.begin
						self.saveend = self.end
						DSsave = True
					if not breakPT:
						self.do_backoff()
						#check difference begin to end before shift begin time
						if DSsave and self.end - self.begin > 3 and self.end - now - self.backoff <= 240:
							breakPT = True
					#breakPT
					if breakPT:
						if self.repeated and DSsave:
							try:
								self.begin = self.savebegin
								self.end = self.saveend
							except:
								pass
						DSsave = False
						return True
					# retry
					oldbegin = self.begin
					self.begin = int(now) + self.backoff
					if abs(self.end - oldbegin) <= 3:
						self.end = self.begin
					else:
						if not self.repeated and self.end < self.begin + 300:
							self.end = self.begin + 300
					return False
				elif not Screens.Standby.inTryQuitMainloop: # not a shutdown messagebox is open
					if self.repeated and DSsave:
						try:
							self.begin = self.savebegin
							self.end = self.saveend
						except:
							pass
					if Screens.Standby.inStandby: # in standby
						print("[PowerTimer] quitMainloop #2")
						quitMainloop(1)
					else:
						callback = self.sendTryQuitMainloopNotification
						message = _("A finished powertimer wants to shutdown your %s %s.\nDo that now?") % (getMachineBrand(), getMachineName())
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = int(config.usage.shutdown_msgbox_timeout.value)
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
				DSsave = False
				return True

		elif next_state == self.StateEnded:
			if self.afterEvent == AFTEREVENT.WAKEUP:
				Screens.Standby.TVinStandby.skipHdmiCecNow('wakeuppowertimer')
				if Screens.Standby.inStandby:
					Screens.Standby.inStandby.Power()
			elif self.afterEvent == AFTEREVENT.STANDBY:
				if not Screens.Standby.inStandby: # not already in standby
					callback = self.sendStandbyNotification
					message = _("A finished powertimer wants to set your\n%s %s to standby. Do that now?") % (getMachineBrand(), getMachineName())
					messageboxtyp = MessageBox.TYPE_YESNO
					timeout = int(config.usage.shutdown_msgbox_timeout.value)
					default = True
					if InfoBar and InfoBar.instance:
						InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
					else:
						Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
			elif self.afterEvent == AFTEREVENT.DEEPSTANDBY:
				if debug:
					print("self.afterEvent == AFTEREVENT.DEEPSTANDBY:")
				#check priority
				prioPT = [TIMERTYPE.WAKEUP, TIMERTYPE.WAKEUPTOSTANDBY, TIMERTYPE.DEEPSTANDBY]
				prioPTae = [AFTEREVENT.WAKEUP, AFTEREVENT.WAKEUPTOSTANDBY, AFTEREVENT.DEEPSTANDBY]
				shiftPT, breakPT = self.getPriorityCheck(prioPT, prioPTae)
				#a timer with higher priority was shifted - no execution of current timer
				if DSsave:
					if debug:
						print("break#1")
					breakPT = True
				#a timer with lower priority was shifted - shift now current timer and wait for restore the saved time values
				if RSsave or RBsave:
					if debug:
						print("shift#1")
					breakPT = False
					shiftPT = True
				#shift or break
				runningPT = False
				#option: check other powertimer is running (current disabled)
				#runningPT = NavigationInstance.instance.PowerTimer.isProcessing(exceptTimer = TIMERTYPE.NONE, endedTimer = self.timerType)
				if isRecTimerWakeup or shiftPT or breakPT or runningPT \
					or NavigationInstance.instance.RecordTimer.isRecording() or abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - now) <= 900 or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - now) <= 900:
					if self.repeated and not aeDSsave:
						self.savebegin = self.begin
						self.saveend = self.end
						aeDSsave = True
					if not breakPT:
						self.do_backoff()
					#breakPT
					if breakPT:
						if self.repeated and aeDSsave:
							try:
								self.begin = self.savebegin
								self.end = self.saveend
							except:
								pass
						aeDSsave = False
						return True
					# retry
					self.end = int(now) + self.backoff
					return False
				elif not Screens.Standby.inTryQuitMainloop: # not a shutdown messagebox is open
					if self.repeated and aeDSsave:
						try:
							self.begin = self.savebegin
							self.end = self.saveend
						except:
							pass
					if Screens.Standby.inStandby: # in standby
						print("[PowerTimer] quitMainloop #5")
						quitMainloop(1)
					else:
						callback = self.sendTryQuitMainloopNotification
						message = _("A finished powertimer wants to shutdown your %s %s.\nDo that now?") % (getMachineBrand(), getMachineName())
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = int(config.usage.shutdown_msgbox_timeout.value)
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
				aeDSsave = False
			NavigationInstance.instance.PowerTimer.saveTimer()
			resetTimerWakeup()
			return True

	def setAutoincreaseEnd(self, entry=None):
		if not self.autoincrease:
			return False
		if entry is None:
			new_end = int(time()) + self.autoincreasetime
		else:
			new_end = entry.begin - 30
		dummyentry = PowerTimerEntry(self.begin, new_end, disabled=True, afterEvent=self.afterEvent, timerType=self.timerType)
		dummyentry.disabled = self.disabled
		timersanitycheck = TimerSanityCheck(NavigationInstance.instance.PowerManager.timer_list, dummyentry)
		if not timersanitycheck.check():
			simulTimerList = timersanitycheck.getSimulTimerList()
			if simulTimerList is not None and len(simulTimerList) > 1:
				new_end = simulTimerList[1].begin
				new_end -= 30				# 30 Sekunden Prepare-Zeit lassen
		if new_end <= time():
			return False
		self.end = new_end
		return True

	def sendStandbyNotification(self, answer):
		self.messageBoxAnswerPending = False
		if answer:
			session = Screens.Standby.Standby
			option = None
			if InfoBar and InfoBar.instance:
				InfoBar.instance.openInfoBarSession(session, option)
			else:
				Notifications.AddNotification(session)

	def sendTryQuitMainloopNotification(self, answer):
		self.messageBoxAnswerPending = False
		if answer:
			session = Screens.Standby.TryQuitMainloop
			option = 1
			if InfoBar and InfoBar.instance:
				InfoBar.instance.openInfoBarSession(session, option)
			else:
				Notifications.AddNotification(session, option)

	def sendTryToRebootNotification(self, answer):
		if answer:
			session = Screens.Standby.TryQuitMainloop
			option = 2
			if InfoBar and InfoBar.instance:
				InfoBar.instance.openInfoBarSession(session, option)
			else:
				Notifications.AddNotification(session, option)

	def sendTryToRestartNotification(self, answer):
		if answer:
			session = Screens.Standby.TryQuitMainloop
			option = 3
			if InfoBar and InfoBar.instance:
				InfoBar.instance.openInfoBarSession(session, option)
			else:
				Notifications.AddNotification(session, option)

	def keyPressed(self, key, tag):
		if self.getAutoSleepWindow():
			self.begin = self.end = int(time()) + int(self.autosleepdelay) * 60

	def getAutoSleepWindow(self):
		now = time()
		if self.autosleepwindow == 'yes':
			if now < self.autosleepbegin and now < self.autosleepend:
				self.begin = self.autosleepbegin
				self.end = self.autosleepend
			elif now > self.autosleepbegin and now > self.autosleepend:
				while self.autosleepend < now:
					self.autosleepend += 86400
				while self.autosleepbegin + 86400 < self.autosleepend:
					self.autosleepbegin += 86400
				self.begin = self.autosleepbegin
				self.end = self.autosleepend
			if not (now > self.autosleepbegin - self.prepare_time - 3 and now < self.autosleepend):
				eActionMap.getInstance().unbindAction('', self.keyPressed)
				self.state = 0
				self.timeChanged()
				return False
		return True

	def getPriorityCheck(self, prioPT, prioPTae):
		shiftPT = breakPT = False
		nextPTlist = NavigationInstance.instance.PowerTimer.getNextPowerManagerTime(getNextTimerTyp=True)
		for entry in nextPTlist:
			#check timers within next 15 mins will started or ended
			if abs(entry[0] - time()) > 900:
				continue
			#faketime
			if entry[1] is None and entry[2] is None and entry[3] is None:
				if debug:
					print("shift#2 - entry is faketime", ctime(entry[0]), entry)
				shiftPT = True
				continue
			#is timer in list itself?
			if entry[0] == self.begin and entry[1] == self.timerType and entry[2] is None and entry[3] == self.state \
				or entry[0] == self.end and entry[1] is None and entry[2] == self.afterEvent and entry[3] == self.state:
				if debug:
					print("entry is itself", ctime(entry[0]), entry)
				nextPTitself = True
			else:
				nextPTitself = False
			if (entry[1] in prioPT or entry[2] in prioPTae) and not nextPTitself:
				if debug:
					print("break#2 <= 900", ctime(entry[0]), entry)
				breakPT = True
				break
		return shiftPT, breakPT

	def getNextActivation(self):
		if self.state == self.StateEnded or self.state == self.StateFailed:
			return self.end

		next_state = self.state + 1

		return {self.StatePrepared: self.start_prepare,
				self.StateRunning: self.begin,
				self.StateEnded: self.end}[next_state]

	def getNextWakeup(self, getNextStbPowerOn=False):
		next_state = self.state + 1
		if getNextStbPowerOn:
			if next_state == 3 and (self.timerType == TIMERTYPE.WAKEUP or self.timerType == TIMERTYPE.WAKEUPTOSTANDBY or self.afterEvent == AFTEREVENT.WAKEUP or self.afterEvent == AFTEREVENT.WAKEUPTOSTANDBY):
				if self.start_prepare > time() and (self.timerType == TIMERTYPE.WAKEUP or self.timerType == TIMERTYPE.WAKEUPTOSTANDBY): #timer start time is later as now - begin time was changed while running timer
					return self.start_prepare
				elif self.begin > time() and (self.timerType == TIMERTYPE.WAKEUP or self.timerType == TIMERTYPE.WAKEUPTOSTANDBY): #timer start time is later as now - begin time was changed while running timer
					return self.begin
				if self.afterEvent == AFTEREVENT.WAKEUP or self.afterEvent == AFTEREVENT.WAKEUPTOSTANDBY:
					return self.end
				next_day = 0
				count_day = 0
				wd_timer = datetime.fromtimestamp(self.begin).isoweekday() * -1
				wd_repeated = bin(128 + self.repeated)
				for s in list(range(wd_timer - 1, -8, -1)):
					count_day += 1
					if int(wd_repeated[s]):
						next_day = s
						break
				if next_day == 0:
					for s in list(range(-1, wd_timer - 1, -1)):
						count_day += 1
						if int(wd_repeated[s]):
							next_day = s
							break
				#return self.begin + 86400 * count_day
				return self.start_prepare + 86400 * count_day
			elif next_state == 2 and (self.timerType == TIMERTYPE.WAKEUP or self.timerType == TIMERTYPE.WAKEUPTOSTANDBY):
				return self.begin
			elif next_state == 1 and (self.timerType == TIMERTYPE.WAKEUP or self.timerType == TIMERTYPE.WAKEUPTOSTANDBY):
				return self.start_prepare
			elif next_state < 3 and (self.afterEvent == AFTEREVENT.WAKEUP or self.afterEvent == AFTEREVENT.WAKEUPTOSTANDBY):
				return self.end
			else:
				return -1

		if self.state == self.StateEnded or self.state == self.StateFailed:
			return self.end
		return {self.StatePrepared: self.start_prepare,
				self.StateRunning: self.begin,
				self.StateEnded: self.end}[next_state]

	def timeChanged(self):
		old_prepare = self.start_prepare
		self.start_prepare = self.begin - self.prepare_time
		self.backoff = 0

		if int(old_prepare) > 60 and int(old_prepare) != int(self.start_prepare):
			self.log(15, "Time changed, start preparing is now %s." % ctime(self.start_prepare))

	def getNetworkAdress(self):
		ret = False
		if self.netip == 'yes':
			try:
				for ip in self.ipadress.split(','):
					if not system("ping -q -w1 -c1 " + ip):
						ret = True
						break
			except:
				print('[PowerTimer] Error reading ip! -> %s' % self.ipadress)
		return ret

	def getNetworkTraffic(self, getInitialValue=False):
		now = time()
		newbytes = 0
		if self.nettraffic == 'yes':
			try:
				if exists('/proc/net/dev'):
					f = open('/proc/net/dev', 'r')
					temp = f.readlines()
					f.close()
					for lines in temp:
						lisp = lines.split()
						if lisp[0].endswith(':') and (lisp[0].startswith('eth') or lisp[0].startswith('wlan')):
							newbytes += int(lisp[1]) + int(lisp[9])
					if getInitialValue:
						self.netbytes = newbytes
						self.netbytes_time = now
						print('[PowerTimer] Receive/Transmit initialBytes=%d, time is %s' % (self.netbytes, ctime(self.netbytes_time)))
						return
					oldbytes = self.netbytes
					seconds = int(now - self.netbytes_time)
					self.netbytes = newbytes
					self.netbytes_time = now
					diffbytes = float(newbytes - oldbytes) * 8 // 1024 // seconds 	#in kbit/s
					if diffbytes < 0:
						print('[PowerTimer] Receive/Transmit -> overflow interface counter, waiting for next value')
						return True
					else:
						print('[PowerTimer] Receive/Transmit kilobits per second: %0.2f (%0.2f MByte in %d seconds), actualBytes=%d, time is %s' % (diffbytes, diffbytes / 8 / 1024 * seconds, seconds, self.netbytes, ctime(self.netbytes_time)))
					if diffbytes > self.trafficlimit:
						return True
			except:
				print('[PowerTimer] Receive/Transmit Bytes: Error reading values! Use "cat /proc/net/dev" for testing on command line.')
		return False


