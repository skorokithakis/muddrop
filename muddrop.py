import elementtree.ElementTree as ET
import exceptions
import re
import sys, os
import base64
import time
from twisted.internet.protocol import Protocol, ClientCreator, ServerFactory
from twisted.protocols.basic import LineReceiver
from twisted.internet import reactor

AC_DISCONNECTED = 0
AC_CONNECTING = 1
AC_CONNECTED = 2

class Formatting:
    """Various text formatting functions."""
    def __init__(self):
        self.strANSICodes = r"(?:\x1b\[(?:(?:\d+(?:|;))*[fHpm]|\=\d*[hl]|\d*[ABCDJKknsu])|\xff(\xfb|\xfc)\x01)"

    def fnGetLineBeginning(self, strLine, intStart):
        """Find the actual beginning of a match object in the line that includes
           the ANSI codes."""

        itrRe = re.finditer(self.strANSICodes, strLine)

        for reMatch in itrRe:
            tplMatch = reMatch.span()
            if tplMatch[0] <= intStart:
                intStart += tplMatch[1] - tplMatch[0]

        return intStart

    def fnTrimNewline(self, strText):
        """Trim the final newlines from a string."""
        if strText.endswith("\r\n") or strText.endswith("\n\r"):
            return strText[:-2]
        elif strText.endswith("\n"):
            return strText[:-1]
        else:
            return strText

    def fnStripANSI(self, strText):
        """Strip various ANSI codes from the input."""
        return re.sub(self.strANSICodes, "", strText)

    def fnGetStyle(self, strLine, intStart):
        """Return the ANSI codes for the matching line."""
        # NOTE: It does not appear that the MUD sends a 0 every time it
        # wants to reset the style. Rather, it assumes that styles are
        # reset whenever it sends a new escape sequence (for example,
        # \x1b[m sets the text to white on black), so we will just use
        # the last style sent.
        reANSI = re.compile(r"\x1b\[(?:(\d+)(?:|;))?(?:(\d+)(?:|;))?(?:(\d+)(?:|;))?(?:(\d+)(?:|;))?(?:(\d+)(?:|;))?m")
        # Return all the styles of the line.
        lstTempStyles = reANSI.findall(strLine[0:intStart])
        if lstTempStyles == []:
            return None

        lstTempStyles = lstTempStyles[-1]
        lstStyles = [int(strNumber) for strNumber in lstTempStyles if strNumber != ""]
        # If there are no foreground/background styles, insert the
        # "white on black" default.
        blnForeground = False
        blnBackground = False
        for intStyle in lstStyles:
            if 30 <= intStyle <= 37:
                blnForeground = True
            if 40 <= intStyle <= 47:
                blnBackground = True
        if not blnForeground:
            lstStyles.append(37)
        if not blnBackground:
            lstStyles.append(40)
        return lstStyles

    def fnExpandMacros(self, strText, insMUDdrop, strIP = "?"):
        """Expand various macros such as the time, server, etc."""
        strText = strText.replace("%server", insMUDdrop.cnfConfiguration.strHost)
        strText = strText.replace("%name", insMUDdrop.cnfConfiguration.strName)
        strText = strText.replace("%rport", str(insMUDdrop.cnfConfiguration.intPort))
        strText = strText.replace("%lport", str(insMUDdrop.cnfConfiguration.intLocalPort))
        strText = strText.replace("%rip", strIP)
        strText = time.strftime(strText)
        return strText

    def fnRegexpify(self, strText):
        """Convert a standard MUSHClient trigger to a regular expression."""
        strText = re.sub(r"([\[\]\-\=\_\+\"\'\;\:\/\?\\\.\>\,\<\!\@\#\$\%\^\&\*\(\)\|])", r"\\\1", strText)
        return "^%s$" % strText

class MUDdrop:
    def init(self, strFilename):
        """Initialise stuff."""
        self.strBuffer = ""
        self.fmFormatting = Formatting()
        self.stConnectionState = AC_DISCONNECTED
        # Remote user connection.
        self.cntClientConnection = None
        self.lstLastStyle = [37, 40]
        # We need the init() function (instead of __init__) for the call
        # below to work, otherwise mdBot will not exist yet and we won't
        # be able to call it.
        self.cnfConfiguration = Configuration(strFilename)
        # Execute the plugins' OnPluginInstall callback.
        self.fnHandleTimers()
        self.cntConnection = MUDConnection(self.cnfConfiguration.strHost, self.cnfConfiguration.intPort)
        mdBot.fnCallPluginFunction("OnPluginInstall", ())

    def fnExecCode(self, strCode, plgPlugin):
        """Execute some code."""
        try:
            exec(strCode, {"world": Callbacks(plgPlugin)})
        except:
            self.fnException(sys.exc_type, sys.exc_value, sys.exc_traceback)

    def fnCallPluginFunction(self, strFunctionCall, tplArguments):
        """Call the specified function in all plugins."""
        for plgPlugin in self.cnfConfiguration.dicPlugins.values():
            plgPlugin.run(strFunctionCall, tplArguments, True)

    def fnExit(self):
        """Handle exiting."""
        if mdBot.cntConnection != None:
            mdBot.cntConnection.close()
        reactor.stop()
        self = None

    def fnHandleTimers(self):
        # The timer that just fired is useless, so create a new one.
        for plgPlugin in self.cnfConfiguration.dicPlugins.values():
            for tmrTimer in plgPlugin.lstTimers:
                if not tmrTimer.blnEnabled:
                    continue
                fltNewTime = time.time()
                if fltNewTime > tmrTimer.fltTime + tmrTimer.intHour * 3600 + tmrTimer.intMinute * 60 + tmrTimer.intSecond:
                    if tmrTimer.intSendTo == 0:
                        if mdBot.cntConnection != None:
                            self.fnSendData(tmrTimer.strSend)
                    elif tmrTimer.intSendTo == 12:
                        self.fnExecCode(tmrTimer.strSend, plgPlugin)
                    if tmrTimer.blnOneShot:
                        plgPlugin.lstTimers.remove(tmrTimer)
                    else:
                        tmrTimer.fltTime = fltNewTime
                    # Check scripting.
                    if tmrTimer.strScript != "":
                        # We need a tuple, hence the comma
                        plgPlugin.run(tmrTimer.strScript, (tmrTimer.strName, ))
        reactor.callLater(1, self.fnHandleTimers)

    def fnMatchTriggers(self, strData):
        lstLastStyle = self.fmFormatting.fnGetStyle(strData, len(strData))
        if lstLastStyle != None:
            self.lstLastStyle = lstLastStyle
        for plgPlugin in self.cnfConfiguration.dicPlugins.values():
            for ciTrigger in plgPlugin.lstTriggers:
                if not ciTrigger.blnEnabled:
                    continue

                # Strip ANSI if necessary
                if ciTrigger.blnKeepANSI:
                    reResult = ciTrigger.reTrigger.search(strData)
                else:
                    reResult = ciTrigger.reTrigger.search(self.fmFormatting.fnStripANSI(strData))

                if reResult != None:
                    if not ciTrigger.blnKeepANSI:
                        intStart = self.fmFormatting.fnGetLineBeginning(strData, reResult.start(0))
                        lstLineStyle = self.fmFormatting.fnGetStyle(strData, intStart)
                        if lstLineStyle == None:
                            lstLineStyle = self.lstLastStyle

                        if ciTrigger.blnMatchBold:
                            if ciTrigger.blnBold != (1 in lstLineStyle):
                                continue
                        if ciTrigger.blnMatchItalic:
                            if ciTrigger.blnItalic != (3 in lstLineStyle):
                                continue
                        if ciTrigger.blnMatchInverse:
                            if ciTrigger.blnInverse != (7 in lstLineStyle):
                                continue
                        if ciTrigger.blnMatchBackColour:
                            if ciTrigger.intBackColour + 32 not in lstLineStyle:
                                continue
                        if ciTrigger.blnMatchTextColour:
                            if ciTrigger.intTextColour + 22 not in lstLineStyle:
                                continue

                    if self.cnfConfiguration.blnDebug:
                        self.fnNoteData("Matched '%s' in %s, groups are %s" % (ciTrigger.strMatch, plgPlugin.strName, reResult.groups()))
                    # Check "send to".
                    if ciTrigger.intSendTo == 0:
                        self.fnSendData(reResult.expand(ciTrigger.strSend))
                    elif ciTrigger.intSendTo == 12:
                        self.fnExecCode(reResult.expand(ciTrigger.strSend), plgPlugin)

                    # Check scripting.
                    if ciTrigger.strScript != "":
                        plgPlugin.run(ciTrigger.strScript, (ciTrigger.strName, strData, reResult.groups()))
                    # Check "keep evaluating".
                    if not ciTrigger.blnKeepEvaluating:
                        break

    def fnProcessData(self, strData):
        """Process the data coming from the MUD and match triggers."""
        self.fnLogDataIn(strData)
        self.fnMatchTriggers(strData)

    def fnSendData(self, strLine):
        """Send data to the MUD."""
        if strLine != "":
            try:
                self.cntConnection.sendLine(strLine)
            except:
                self.fnError("Could not write data to socket. Reason is '%s'." % sys.exc_value)
            self.fnLogDataOut(strLine)

    def fnLogDataIn(self, strData):
        """Log the incoming data."""
        if self.cnfConfiguration.blnToScreen:
            print self.fmFormatting.fnStripANSI(strData)
        if self.cnfConfiguration.blnLogging:
            if not self.cnfConfiguration.blnKeepANSI:
                self.cnfConfiguration.flLogFile.write("%s%s%s\n" % (self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut, self), self.fmFormatting.fnStripANSI(strData), self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendIn, self)))
            else:
                self.cnfConfiguration.flLogFile.write("%s%s%s\n" % (self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut, self), strData, self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendIn, self)))

    def fnNoteData(self, strLine, blnOmitConsole = False, blnOmitRemote = False, blnOmitLog = False, blnOmitNewline = False):
        """Print debugging data."""
        strData = self.fmFormatting.fnTrimNewline(strLine)
        if self.cnfConfiguration.blnNoteToConsole and not blnOmitConsole:
            if blnOmitNewline:
                print strData,
            else:
                print strData
        if self.cnfConfiguration.blnLogging and not blnOmitLog and self.cnfConfiguration.blnNoteToLog:
            self.cnfConfiguration.flLogFile.write("%s%s%s%s" % (self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut, self), strData, self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendOut, self), (blnOmitNewline and [""] or ["\n"])[0]))
        if self.cntClientConnection and blnOmitRemote and self.cnfConfiguration.blnNoteToRemote:
            self.cntClientConnection.fnSend(strData + (blnOmitNewline and [""] or ["\n"])[0])

    def fnLogDataOut(self, strData):
        """Log the outgoing data."""
        strData = self.fmFormatting.fnTrimNewline(strData)
        if self.cnfConfiguration.blnToScreen:
            print strData
        if self.cnfConfiguration.blnLogging:
            self.cnfConfiguration.flLogFile.write("%s%s%s\n" % (self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut, self), strData, self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendOut, self)))

    def fnGetStyle(self, strLine, intCharNumber):
        """Retrieve the style of a character in a line."""
        blnEscaped = False
        strBuffer = ""
        intCounterClear = 0
        for strCharacter in strLine:
            if not blnEscaped:
                if strCharacter == "\x1b":
                    blnEscaped = True
                    lstStyle = []
                else:
                    intCounterClear +=1
            else:
                if strCharacter in "0123456789":
                    strBuffer += strCharacter
                elif strCharacter == "m":
                    blnEscaped = False
                    lstStyle.append(strBuffer and int(strBuffer))
                    strBuffer = ""
                elif strCharacter == ";":
                    lstStyle.append(strBuffer and int(strBuffer))
                    strBuffer = ""
            if intCounterClear == intCharNumber:
                return lstStyle

    def OnConnect(self):
        """Initialise various connection details."""
        # Open the logfile if specified.
        if self.cnfConfiguration.blnLogging:
            self.cnfConfiguration.flLogFile = file(self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strLogFile, self), "a")
        # Print OnConnect string.
        if self.cnfConfiguration.strOnConnect != "":
            self.fnNoteData(self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnConnect, self) + "\n")
        # Perform autologon.
        self.fnSendData(self.cnfConfiguration.strName)
        self.cntConnection.sendLine(self.cnfConfiguration.strPassword + "\n")
        # Send connection commands.
        if self.cnfConfiguration.strConnectionCommands != "":
            self.fnSendData(self.cnfConfiguration.strConnectionCommands)
        # Execute OnPluginConnect.
        self.fnCallPluginFunction("OnPluginConnect", ())

    def OnConnectFailed(self):
        """Do stuff when the connection has failed."""
        # Print OnConnectFailed string.
        if self.cnfConfiguration.strOnConnectFailed != "":
            self.fnNoteData(self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnConnectFailed, self) + "\n")
        self.fnCallPluginFunction("OnPluginConnectFailed", ())

    def OnDisconnect(self):
        """Clean up after disconnection."""
        if self.cnfConfiguration.strOnDisconnect != "":
            self.fnNoteData(self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnDisconnect, self) + "\n")
        if self.cnfConfiguration.blnLogging:
            self.cnfConfiguration.flLogFile.close()
        self.fnCallPluginFunction("OnPluginDisconnect", ())

    def OnRemoteConnect(self, strAddress):
        """Remote client connection callback."""
        if self.cnfConfiguration.strOnRemoteConnect != "":
            self.fnNoteData(self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnRemoteConnect, self, strAddress), blnOmitRemote = True)
        self.fnCallPluginFunction("OnPluginClientConnect", ())

    def OnRemoteDisconnect(self, strAddress):
        """Remote client disconnection callback."""
        if self.cnfConfiguration.strOnRemoteDisconnect != "":
            self.fnNoteData(self.fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnRemoteDisconnect, self, strAddress), blnOmitRemote = True)
        self.fnCallPluginFunction("OnPluginClientDisconnect", ())

    def fnError(self, strDescription):
        print "ERROR: %s" % strDescription
        self.fnExit()

    def fnException(self, strType, strValue, tbTraceback):
        if strType == exceptions.SystemExit:
            os._exit(0)
        print "Exception of type %s occurred in line %s, reason \"%s\"." % (strType, tbTraceback.tb_lineno, strValue)

class Callbacks:
    def __init__(self, plgNamespace):
        # Get the plugin reference so we can manipulate it.
        self.plgPlugin = plgNamespace
    def Connect(self):
        """Connect to the server."""
        if mdBot.cntConnection == None:
            mdBot.cntConnection = MUDConnection(mdBot.cnfConfiguration.strHost, mdBot.cnfConfiguration.intPort)
    def Send(self, strData):
        """Send data to the world."""
        mdBot.fnSendData(strData)
    def Exit(self):
        """Exits the program."""
        mdBot.fnExit()
    def Disconnect(self):
        """Disconnect the current connection."""
        if mdBot.cntConnection != None:
            mdBot.cntConnection.close()
    def Note(self, strData):
        """Send text to stdout."""
        mdBot.fnNoteData(strData)
    def SetVariable(self, strVariableName, strData):
        """Set a variable in the plugin's variables dictionary."""
        self.plgPlugin.dicVariables[strVariableName] = strData
        return 0
    def GetInfo(self, intInfoType):
        """Get information about the current character."""
        if intInfoType == 1:
            return mdBot.cnfConfiguration.strHost
        elif intInfoType == 3:
            return mdBot.cnfConfiguration.strName
        elif intInfoType == 11:
            return mdBot.cnfConfiguration.strOnConnect
        elif intInfoType == 12:
            return mdBot.cnfConfiguration.strOnDisconnect
        elif intInfoType == 106:
            # Return true if not connected.
            return (mdBot.stConnectionState == AC_DISCONNECTED) and True or False
        elif intInfoType == 106:
            # Return true if currently connecting.
            return (mdBot.stConnectionState == AC_CONNECTING) and True or False
        elif intInfoType == 3:
            return mdBot.cnfConfiguration.strName
        else:
            return "NOT IMPLEMENTED"
    def GetVariable(self, strVariableName):
        """Get a variable from the plugin's variables dictionary."""
        if strVariableName in self.plgPlugin.dicVariables:
            varReturn = self.plgPlugin.dicVariables[strVariableName]
        else:
            varReturn = None
        return varReturn
    def DeleteVariable(self, strVariableName):
        """Delete a variable from the plugin's variables dictionary."""
        if strVariableName in self.plgPlugin.dicVariables:
            del self.plgPlugin.dicVariables[strVariableName]
            varReturn = 0
        else:
            varReturn = 30019
        return varReturn
    def EnableTrigger(self, strTriggerName, blnEnabled):
        """Enable or disable a trigger."""
        for ciTrigger in self.plgPlugin.lstTriggers:
            if ciTrigger.strName == strTriggerName:
                # If the trigger is what the user wanted, set its status
                ciTrigger.blnEnabled = blnEnabled
                return 0
        else:
            # Trigger not found
            return 30005
    def EnableTriggerGroup(self, strGroupName, blnEnabled):
        """Enable or disable a trigger group."""
        intCounter = 0
        for ciTrigger in self.plgPlugin.lstTriggers:
            if ciTrigger.strGroup == strGroupName:
                # If the trigger is in the group, set its status
                # and increment the counter.
                ciTrigger.blnEnabled = blnEnabled
                intCounter += 1
        return intCounter
    def EnableTimer(self, strTimerName, blnEnabled):
        """Enable or disable a timer."""
        for ciTimer in self.plgPlugin.lstTimers:
            if ciTimer.strName == strTimerName:
                # If the trigger is what the user wanted, set its status
                ciTimer.fltTime = time.time() + ciTimer.intOffsetHour * 3600 + ciTimer.intOffsetMinute * 60 + ciTimer.intOffsetSecond
                ciTimer.blnEnabled = blnEnabled
                return 0
        else:
            # Trigger not found
            return 30017
    def EnableTimerGroup(self, strGroupName, blnEnabled):
        """Enable or disable a trigger group."""
        intCounter = 0
        for ciTimer in self.plgPlugin.lstTimers:
            if ciTimer.strGroup == strGroupName:
                # If the trigger is in the group, set its status
                # and increment the counter.
                ciTimer.fltTime = time.time() + ciTimer.intOffsetHour * 3600 + ciTimer.intOffsetMinute * 60 + ciTimer.intOffsetSecond
                ciTimer.blnEnabled = blnEnabled
                intCounter += 1
        return intCounter
    def EnableGroup(self, strGroupName, blnEnabled):
        """Enable or disable a group."""
        intCounter = 0
        for ciTrigger in self.plgPlugin.lstTriggers:
            if ciTrigger.strGroup == strGroupName:
                # If the trigger is in the group, set its status
                # and increment the counter.
                ciTrigger.blnEnabled = blnEnabled
                intCounter += 1
        for ciTimer in self.plgPlugin.lstTimers:
            if ciTimer.strGroup == strGroupName:
                # If the trigger is in the group, set its status
                # and increment the counter.
                ciTimer.blnEnabled = blnEnabled
                intCounter += 1
        # TODO: Add aliases.
        return intCounter
    def TraceOut(self, strMessage):
        mdBot.fnNoteData("%s notes '%s'." % (self.plgPlugin.strName, strMessage))
    def GetPluginName(self):
        """Return the plugin's name."""
        return self.plgPlugin.strName
    def ColourTell(self, strForegroundColor, strBackgroundColor, strText):
        """Send text to the console without a newline, in color."""
        # TODO: Make color actually work.
        mdBot.fnNoteData(strText, blnOmitNewline = True)
    def ColourNote(self, strForegroundColor, strBackgroundColor, strText):
        """Send text to the console with a newline, in color."""
        # TODO: Make color actually work.
        mdBot.fnNoteData(strText)
    def DoAfter(self, intSeconds, strText):
        """Send text to the MUD after the specified amount of seconds."""
        self.plgPlugin.createtimer(int(intSeconds), strText)

class Plugin:
    class Trigger:
        """The trigger object class."""
        def __repr__(self):
            return self.strMatch
        def __cmp__(self, other):
            if self.intSequence < other.intSequence:
                return -1
            elif self.intSequence == other.intSequence:
                return 0
            else:
                return 1
    class Timer:
        """The timer object class."""

    def createtimer(self, intSeconds, strText):
        """Create a timer."""
        tmrTimer = Plugin.Timer()
        tmrTimer.blnEnabled = True
        tmrTimer.intHour = 0
        tmrTimer.intMinute = 0
        tmrTimer.intSecond = intSeconds
        tmrTimer.blnOneShot = True
        tmrTimer.fltTime = time.time()
        tmrTimer.strSend = strText
        self.lstTimers.append(tmrTimer)

    def loadtimers(self, xmlTimers):
        """Load timers from the xmlTimers node."""
        self.lstTimers = []
        if xmlTimers == None:
            return
        for xmlTimer in xmlTimers:
            tmrTimer = Plugin.Timer()
            tmrTimer.blnEnabled = self.getxmlattr(xmlTimer, "enabled", True)
            tmrTimer.strName = self.getxmlattr(xmlTimer, "name")
            for tmrOther in self.lstTimers:
                if (tmrOther.strName.lower() == tmrOther.strName.lower()) and (tmrOther.strName != ""):
                    mdBot.fnError("Duplicate timer name found: '%s'" % tmrOther.strName)
            tmrTimer.strGroup = self.getxmlattr(xmlTimer, "group")
            tmrTimer.strVariable = self.getxmlattr(xmlTimer, "variable")
            tmrTimer.strScript = self.getxmlattr(xmlTimer, "script")
            tmrTimer.intHour = int(self.getxmlattr(xmlTimer, "hour"))
            tmrTimer.intMinute = int(self.getxmlattr(xmlTimer, "minute"))
            # This is converted to an int, change if accuracy is needed.
            tmrTimer.intSecond = int(float(self.getxmlattr(xmlTimer, "second")))
            tmrTimer.intSendTo = int(self.getxmlattr(xmlTimer, "send_to"))
            tmrTimer.intOffsetHour = int(self.getxmlattr(xmlTimer, "offset_hour"))
            tmrTimer.intOffsetMinute = int(self.getxmlattr(xmlTimer, "offset_minute"))
            tmrTimer.intOffsetSecond = int(self.getxmlattr(xmlTimer, "offset_second"))
            tmrTimer.blnOneShot = self.getxmlattr(xmlTimer, "one_shot", True)
            tmrTimer.blnOmitFromOutput = self.getxmlattr(xmlTimer, "omit_from_output", True)
            tmrTimer.blnOmitFromLog = self.getxmlattr(xmlTimer, "omit_from_log", True)
            tmrTimer.blnActiveClosed = self.getxmlattr(xmlTimer, "active_closed", True)
            tmrTimer.blnAtTime = self.getxmlattr(xmlTimer, "at_time", True)
            tmrTimer.strSend = xmlTimer.find("send").text
            tmrTimer.fltTime = time.time() + tmrTimer.intOffsetHour * 3600 + tmrTimer.intOffsetMinute * 60 + tmrTimer.intOffsetSecond

            if tmrTimer.intHour + tmrTimer.intMinute + tmrTimer.intSecond > 0:
                # Append it to the timers list.
                self.lstTimers.append(tmrTimer)
            else:
                mdBot.fnError("Timer has no interval set.")

    def loadtriggers(self, xmlTriggers):
        """Load triggers from the xmlTriggers node."""
        fmFormatting = Formatting()

        self.lstTriggers = []
        if xmlTriggers == None:
            return
        for xmlTrigger in xmlTriggers:
            trgTrigger = Plugin.Trigger()
            trgTrigger.strMatch = self.getxmlattr(xmlTrigger, "match")
            trgTrigger.blnEnabled = self.getxmlattr(xmlTrigger, "enabled", True)
            trgTrigger.blnKeepANSI = self.getxmlattr(xmlTrigger, "keep_ansi", True)
            trgTrigger.blnBold = self.getxmlattr(xmlTrigger, "bold", True)
            trgTrigger.blnInverse = self.getxmlattr(xmlTrigger, "inverse", True)
            trgTrigger.blnItalic = self.getxmlattr(xmlTrigger, "italic", True)
            trgTrigger.blnMatchBackColour = self.getxmlattr(xmlTrigger, "match_back_colour", True)
            trgTrigger.blnMatchBold = self.getxmlattr(xmlTrigger, "match_bold", True)
            trgTrigger.blnMatchInverse = self.getxmlattr(xmlTrigger, "match_inverse", True)
            trgTrigger.blnMatchItalic = self.getxmlattr(xmlTrigger, "match_italic", True)
            trgTrigger.blnMatchTextColour = self.getxmlattr(xmlTrigger, "match_text_colour", True)
            trgTrigger.strName = self.getxmlattr(xmlTrigger, "name")
            for trgOther in self.lstTriggers:
                if (trgOther.strName.lower() == trgTrigger.strName.lower()) and (trgOther.strName != ""):
                    mdBot.fnError("Duplicate trigger name found: '%s'" % trgOther.strName)
            trgTrigger.strGroup = self.getxmlattr(xmlTrigger, "group")
            trgTrigger.blnIgnoreCase = self.getxmlattr(xmlTrigger, "ignore_case", True)
            trgTrigger.blnRegexp = self.getxmlattr(xmlTrigger, "regexp", True)
            trgTrigger.blnKeepEvaluating = self.getxmlattr(xmlTrigger, "keep_evaluating", True)
            trgTrigger.intSequence = int(self.getxmlattr(xmlTrigger, "sequence"))
            trgTrigger.intBackColour = int(self.getxmlattr(xmlTrigger, "back_colour"))
            trgTrigger.intTextColour = int(self.getxmlattr(xmlTrigger, "text_colour"))
            trgTrigger.intSendTo = int(self.getxmlattr(xmlTrigger, "send_to"))
            trgTrigger.strScript = self.getxmlattr(xmlTrigger, "script")

            if not trgTrigger.blnRegexp:
                trgTrigger.strMatch = fmFormatting.fnRegexpify(trgTrigger.strMatch)

            if len(xmlTrigger) > 0:
                # Substitute MUSHClient compatible %1 for pyregexp \1.
                trgTrigger.strSend = re.sub(r"\%(?:(\d)|\<(.*?)\>)", r"\\g<\1>", xmlTrigger[0].text)
            else:
                trgTrigger.strSend = ""
            intFlags = 0
            if trgTrigger.blnIgnoreCase:
                intFlags |= re.IGNORECASE
            trgTrigger.reTrigger = re.compile(trgTrigger.strMatch, intFlags)
            self.lstTriggers.append(trgTrigger)
        self.lstTriggers.sort()

    def run(self, strFunctionName, tplArguments, blnSilent = False):
        """Execute the function in the plugin namespace."""
        try:
            self.dicGlobals[strFunctionName](*tplArguments)
        except KeyError:
            if not blnSilent:
                mdBot.fnException(sys.exc_type, sys.exc_value, sys.exc_traceback)
        except:
                mdBot.fnException(sys.exc_type, sys.exc_value, sys.exc_traceback)

    def load(self, strFilename):
        """Load the plugin data, triggers, etc."""
        try:
            flPlugin = file(strFilename)
        except:
            mdBot.fnError("Plugin '%s' cannot be opened." % strFilename)

        xmlTree = ET.parse(flPlugin)
        xmlRoot = xmlTree.getroot()

        # Load generic plugin configuration.
        xmlPlugin = xmlTree.find("plugin")
        self.strName = self.getxmlattr(xmlPlugin, "name")
        self.strID = self.getxmlattr(xmlPlugin, "id")
        self.blnSaveState = self.getxmlattr(xmlPlugin, "save_state", True)
        if self.getxmlattr(xmlPlugin, "language").lower() != "python":
            mdBot.fnError("The only plugin language supported is Python, error in '%s'." % strFilename)
        strScript = xmlTree.find("script").text
        # Execute the script and keep the globals
        self.dicGlobals = {"world": Callbacks(self)}
        try:
            exec(strScript, self.dicGlobals)
        except:
            mdBot.fnException(sys.exc_type, sys.exc_value, sys.exc_traceback)
        self.dicVariables = {}

        # Load triggers and timers.
        self.loadtriggers(xmlRoot.find("triggers"))
        self.loadtimers(xmlRoot.find("timers"))
        flPlugin.close()

        return self.strID

    def fnselyn(self, strText):
        """Convert 'y'/'n' to True or False."""
        if strText.lower() == "y":
            return True
        else:
            return False

    def getxmlattr(self, xmlNode, strAttribute, blnYN = False):
        """Get the value of strAttribute from xmlNode, converting it to
           binary if blnYN is True."""

        dicAttributes = {
            "appendin": "",               # Text to append to incoming data.
            "appendout": "",              # Text to append to outgoing data.
            "active_closed": False,       # Is timer active when the world is closed? (NS)
            "at_time": False,             # At time for timer. (NS)
            "back_colour": 0,             # Backcolour to match on. (NS)
            "bold": False,                # Match if the text is bold. (NS)
            "connectioncommands": "",     # Commands to send on connection.
            "debug": False,               # Print debugging data in the output.
            "enabled": False,             # Is the item enabled?
            "expand_variables": False,    # Expand variables. (NS)
            "group": "",                  # Item group name.
            "hour": 0,                    # Hour interval for timers. (NS)
            "ignore_case": False,         # Ignore case. (NS)
            "inverse": False,             # Match if the text is inverse. (NS)
            "italic": False,              # Match if the text is italic. (NS)
            "keep_ansi": False,           # Keep the ANSI codes to match on.
            "keep_evaluating": True,      # Keep evaluating after a trigger has been matched.
            "localport": 4000,            # Port number to listen to.
            "logfile": "log.txt",         # Log filename.
            "logging": False,             # Is logging enabled?
            "match_back_colour": False,   # Enable match on backcolour.
            "match_bold": False,          # Enable match on bold.
            "match_inverse": False,       # Enable match on inverse.
            "match_italic": False,        # Enable match on italic.
            "match_text_colour": False,   # Enable match on forecolour.
            "minute": 0,                  # Minute interval for timers. (NS)
            "name": "",                   # Item name.
            "notetoconsole": False,       # Send notes to the console.
            "notetolog": False,           # Write notes to the log.
            "notetoremote": True,         # Send notes to the remote client.
            "offset_hour": 0,             # Hour offset for timers. (NS)
            "offset_minute": 0,           # Minute offset for timers. (NS)
            "offset_second": 0,           # Second offset for timers. (NS)
            "omit_from_log": False,       # Omit line from logfile. (NS)
            "omit_from_output": False,    # Omit line from output. (NS)
            "one_shot": False,            # Is timer one-shot? (NS)
            "onconnect": "",              # Log text to append on connection.
            "ondisconnect": "",           # Log text to append on disconnection.
            "onremoteconnect": "",        # Log text to append on remote client connection.
            "onremotedisconnect": "",     # Log text to append on remote client disconnection.
            "port": 4000,                 # Port to connect on.
            "prependin": "",              # Text to prepend to incoming data.
            "prependout": "",             # Text to prepend to outgoing data.
            "regexp": False,              # Is the trigger a regular expression?
            "repeat": False,              # Repeat the trigger on the same line. (NS)
            "save_state": False,          # Save the namespace's state. (NS)
            "script": "",                 # Call a script function when executed.
            "second": 0,                  # Second interval for timers. (NS)
            "send_to": 0,                 # Send to various outputs. (PS)
            "sequence": 100,              # Sequence of the trigger.
            "toscreen": False,            # Print the output to stdout.
            "text_colour": 0,             # Forecolour to match on. (NS)
            "variable": "",               # Variable to send to (NS)
        }
        if strAttribute in xmlNode.attrib:
            if blnYN:
                return self.fnselyn(xmlNode.attrib[strAttribute])
            else:
                return xmlNode.attrib[strAttribute]
        else:
            if strAttribute in dicAttributes:
                return dicAttributes[strAttribute]
            else:
                mdBot.fnError("Mandatory attribute \"%s\" does not exist." % strAttribute)


class Configuration:
    def __init__(self, strFilename):
        """Load the configuration from the specified file."""
        try:
            flFile = file(strFilename)
        except:
            print("Cannot open file '%s' for reading." % strFilename)
            sys.exit()
        xmlTree = ET.parse(flFile)
        xmlRoot = xmlTree.getroot()
        # Create this here so we can access the getxmlattr function.
        self.dicPlugins = {"000000000000000000000000": Plugin()}
        plgNamespace = self.dicPlugins["000000000000000000000000"]

        # Connection details.
        self.strHost = plgNamespace.getxmlattr(xmlRoot, "host")
        self.intPort = int(plgNamespace.getxmlattr(xmlRoot, "port"))
        self.intLocalPort = int(plgNamespace.getxmlattr(xmlRoot, "localport"))
        self.strName = plgNamespace.getxmlattr(xmlRoot, "name")
        self.strPassword = base64.decodestring(plgNamespace.getxmlattr(xmlRoot, "password"))
        self.strConnectionCommands = plgNamespace.getxmlattr(xmlRoot, "connectioncommands").decode("string_escape")

        # Debugging.
        self.blnDebug = plgNamespace.getxmlattr(xmlRoot, "debug", True)
        self.blnNoteToConsole = plgNamespace.getxmlattr(xmlRoot, "notetoconsole", True)
        self.blnNoteToLog = plgNamespace.getxmlattr(xmlRoot, "notetolog", True)
        self.blnNoteToRemote = plgNamespace.getxmlattr(xmlRoot, "notetoremote", True)

        # Logging.
        xmlLogging = xmlRoot.find("logging")
        self.blnLogging = plgNamespace.getxmlattr(xmlLogging, "enabled", True)
        self.blnToScreen = plgNamespace.getxmlattr(xmlLogging, "toscreen", True)
        self.blnKeepANSI = plgNamespace.getxmlattr(xmlLogging, "keep_ansi", True)
        self.strLogFile = plgNamespace.getxmlattr(xmlLogging, "logfile")
        self.strAppendIn = plgNamespace.getxmlattr(xmlLogging, "appendin").decode("string_escape")
        self.strAppendOut = plgNamespace.getxmlattr(xmlLogging, "appendout").decode("string_escape")
        self.strPrependIn = plgNamespace.getxmlattr(xmlLogging, "prependin").decode("string_escape")
        self.strPrependOut = plgNamespace.getxmlattr(xmlLogging, "prependout").decode("string_escape")
        self.strOnConnect = plgNamespace.getxmlattr(xmlLogging, "onconnect").decode("string_escape")
        self.strOnConnectFailed = plgNamespace.getxmlattr(xmlLogging, "onconnectfailed").decode("string_escape")
        self.strOnDisconnect = plgNamespace.getxmlattr(xmlLogging, "ondisconnect").decode("string_escape")
        self.strOnRemoteConnect = plgNamespace.getxmlattr(xmlLogging, "onremoteconnect").decode("string_escape")
        self.strOnRemoteDisconnect = plgNamespace.getxmlattr(xmlLogging, "onremotedisconnect").decode("string_escape")

        # Pass the standard namespace to the plugin so we can access it.
        plgNamespace.strID = "000000000000000000000000"
        plgNamespace.strName = "Main namespace"
        plgNamespace.blnSaveState = True
        strScript = xmlTree.find("script").text
        plgNamespace.dicGlobals = {"world": Callbacks(plgNamespace)}
        try:
            exec(strScript, plgNamespace.dicGlobals)
        except:
            mdBot.fnException(sys.exc_type, sys.exc_value, sys.exc_traceback)
        plgNamespace.dicVariables = {}

        # Load triggers, timers.
        plgNamespace.loadtriggers(xmlRoot.find("triggers"))
        plgNamespace.loadtimers(xmlRoot.find("timers"))

        # Load plugins.
        if xmlRoot.find("plugins"):
            for xmlPlugin in xmlRoot.find("plugins"):
                plgPlugin = Plugin()
                strID = plgPlugin.load(xmlPlugin.attrib["name"])
                if strID in self.dicPlugins:
                    mdBot.fnError("Duplicate plugin '%s' found." % xmlPlugin.attrib["name"])
                else:
                    self.dicPlugins[strID] = plgPlugin
        flFile.close()

class MUDProtocol(LineReceiver):
    def __init__(self):
        self.setRawMode()
        self.delimiter = "\n"
        self.buffer = ""
    def rawDataReceived(self, data):
        if mdBot.cntClientConnection:
            mdBot.cntClientConnection.fnSend(data)
        self.buffer += data.replace("\r", "")
        while True:
            try:
                line, self.buffer = self.buffer.split(self.delimiter, 1)
            except ValueError:
                break
            else:
                mdBot.fnProcessData(line.replace("\r", ""))
    def connectionLost(self, reason):
        mdBot.stConnectionState = AC_DISCONNECTED
        mdBot.cntConnection.cleanup()
        mdBot.OnDisconnect()

class MUDConnection:
    def __init__(self, host, port):
        self.protocol = None
        creator = ClientCreator(reactor, MUDProtocol)
        deferred = creator.connectTCP(host, port)
        deferred.addCallback(self.builtProtocol)
        deferred.addErrback(self.connectionFailed)
    def connectionFailed(self, reason):
        mdBot.stConnectionState = AC_DISCONNECTED
        self.cleanup()
        mdBot.OnConnectFailed()
    def builtProtocol(self, protocol):
        self.protocol = protocol
        mdBot.stConnectionState = AC_CONNECTED
        mdBot.cntClientConnection = MUDServer()
        mdBot.OnConnect()
    def startedConnecting(self, connector):
        print "Started to connect."
        mdBot.stConnectionState = AC_CONNECTING
    def sendLine(self, line):
        self.protocol.sendLine(line)
    def close(self):
        self.protocol.transport.loseConnection()
    def cleanup(self):
        mdBot.stConnectionState = AC_DISCONNECTED
        mdBot.cntConnection = None
        if mdBot.cntClientConnection:
            mdBot.cntClientConnection.close()
            mdBot.cntClientConnection = None

class MUDServerProtocol(LineReceiver):
    def lineReceived(self, line):
        if self.intState == 0:
            self.strAuthName = line
            self.intState = 1
            self.sendLine("Please enter the character password:")
        elif self.intState == 1:
            if self.strAuthName.lower() == mdBot.cnfConfiguration.strName.lower() and line == mdBot.cnfConfiguration.strPassword:
                self.intState = 2
                self.sendLine("Welcome to %s.\n" % mdBot.cnfConfiguration.strName)
                mdBot.OnRemoteConnect(self.transport.getPeer().host)
            else:
                self.sendLine("Wrong name/password.\n")
                self.transport.loseConnection()
        elif self.intState == 2:
            mdBot.cntConnection.sendLine(line)
            mdBot.fnLogDataOut(line)
    def connectionLost(self, reason):
        if self.intState == 2:
            mdBot.OnRemoteDisconnect(self.transport.getPeer().host)
        self.factory.connected = False
        if mdBot.cntConnection:
            mdBot.cntClientConnection = MUDServer()
    def connectionMade(self):
        self.factory.connected = True
        self.factory.port.stopListening()
        self.intState = 0 # Name auth
        self.sendLine("You have connected to MUDdrop, you have 20 seconds to authenticate. Please enter\nthe character name:")
        reactor.callLater(20, self.factory.timeout)

class MUDServer(ServerFactory):
    def __init__(self):
        self.connected = False
        self.client = None
        self.port = reactor.listenTCP(mdBot.cnfConfiguration.intLocalPort, self)
    def buildProtocol(self, addr):
        self.client = MUDServerProtocol()
        self.client.factory = self
        return self.client
    def fnSend(self, line):
        if self.connected and self.client.intState == 2: # Authenticated.
            self.client.transport.write(line)
    def close(self):
        if self.connected:
            self.client.transport.loseConnection()
    def timeout(self):
        if self.client.intState != 2:
            self.close()

try:
    flFile = file("muddrop.xml")
except:
    print("Cannot open configuration file 'muddrop.xml' for reading.")
    sys.exit()
xmlTree = ET.parse(flFile)
flFile.close()
xmlRoot = xmlTree.getroot()

mdBot = MUDdrop()
mdBot.init(xmlRoot[0].attrib["file"])
del xmlTree, flFile
del xmlRoot

reactor.run()
