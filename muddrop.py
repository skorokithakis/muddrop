import elementtree.ElementTree as ET
import threading
import re
import sys
import base64
import time
import asyncore
import asynchat
import socket

class Formatting:
    """Various text formatting functions."""
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
        strReplaced = strText
        strReplaced = re.sub(r"\x1b\[(\d+(|;))*m", "", strReplaced)
        strReplaced = re.sub(r"\x1b\[(2J|k|s|u)", "", strReplaced)
        strReplaced = re.sub(r"\xff(\xfb|\xfc)\x01", "", strReplaced)
        return strReplaced

    def fnExpandMacros(self, strText, strIP = "?"):
        """Expand various macros such as the time, server, etc."""
        strText = strText.replace("%server", mdBot.cnfConfiguration.strHost)
        strText = strText.replace("%name", mdBot.cnfConfiguration.strName)
        strText = strText.replace("%rport", str(mdBot.cnfConfiguration.intPort))
        strText = strText.replace("%lport", str(mdBot.cnfConfiguration.intLocalPort))
        strText = strText.replace("%rip", strIP)
        strText = time.strftime(strText)
        return strText

    def fnRegexpify(self, strText):
        """Convert a standard MUSHClient trigger to a regular expression."""
        strText = re.sub(r"([\[\]\-\=\_\+\"\'\;\:\/\?\\\.\>\,\<\!\@\#\$\%\^\&\*\(\)\|])", r"\\\1", strText)
        return "^%s$" % strText

class MUDdrop:
    def init(self):
        """Initialise stuff."""
        self.strBuffer = ""
        self.cnnConnection = None
        # We need the init() function (instead of __init__) for the call
        # below to work, otherwise mdBot will not exist yet and we won't
        # be able to call it.
        self.cnfConfiguration = Configuration(r"character.xml")
        self.cntConnection = None
        self.fnHandleTimers()

    def fnExit(self):
        """Handle exiting."""
        global tmrThreadTimer
        if tmrThreadTimer:
            tmrThreadTimer.cancel()
            tmrThreadTimer = None
        mdBot = None
        sys.exit()

    def fnHandleTimers(self):
        global tmrThreadTimer
        tmrThreadTimer = threading.Timer(1.0, self.fnHandleTimers)
        for plgPlugin in self.cnfConfiguration.dicPlugins.values():
            for tmrTimer in plgPlugin.lstTimers:
                fltNewTime = time.time()
                if fltNewTime > tmrTimer.fltTime + tmrTimer.intHour * 3600 + tmrTimer.intMinute * 60 + tmrTimer.intSecond:
                    self.fnSendData(tmrTimer.strSend)
                    if tmrTimer.blnOneShot:
                        plgPlugin.lstTimers.remove(tmrTimer)
                    else:
                        tmrTimer.fltTime = fltNewTime

        tmrThreadTimer.start()

    def fnMatchTriggers(self, strData):
        for plgPlugin in self.cnfConfiguration.dicPlugins.values():
            for ciTrigger in plgPlugin.lstTriggers:
                if ciTrigger.blnEnabled == False:
                    continue

                # Strip ANSI if necessary
                if ciTrigger.blnKeepANSI:
                    reResult = ciTrigger.reTrigger.search(strData)
                else:
                    reResult = ciTrigger.reTrigger.search(fmFormatting.fnStripANSI(strData))

                if reResult != None:
                    if self.cnfConfiguration.blnDebug:
                        self.fnNoteData("Matched '%s' in %s, groups are %s" % (ciTrigger.strMatch, plgPlugin.strName, reResult.groups()))
                    # Check "send to".
                    if ciTrigger.intSendTo == 0:
                        self.fnSendData(reResult.expand(ciTrigger.strSend))
                    elif ciTrigger.intSendTo == 12:
                        try:
                            exec(reResult.expand(ciTrigger.strSend), {"world": Callbacks(plgPlugin)})
                        except:
                            mdBot.fnException(sys.exc_type, sys.exc_value, sys.exc_traceback)

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
                self.cntConnection.push(strLine + "\n")
            except:
                mdBot.fnError("Could not write data to socket. Reason is '%s'." % sys.exc_value)
            self.fnLogDataOut(strLine)

    def fnLogDataIn(self, strData):
        """Log the incoming data."""
        if self.cnfConfiguration.blnToScreen:
            print fmFormatting.fnStripANSI(strData)
        if self.cnfConfiguration.blnLogging:
            if not self.cnfConfiguration.blnKeepANSI:
                self.cnfConfiguration.flLogFile.write("%s%s%s\n" % (fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut), fmFormatting.fnStripANSI(strData), fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendIn)))
            else:
                self.cnfConfiguration.flLogFile.write("%s%s%s\n" % (fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut), strData, fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendIn)))

    def fnNoteData(self, strLine, blnOmitConsole = False, blnOmitRemote = False, blnOmitLog = False, blnOmitNewline = False):
        """Print debugging data."""
        strData = fmFormatting.fnTrimNewline(strLine)
        if self.cnfConfiguration.blnNoteToConsole and not blnOmitConsole:
            if blnOmitNewline:
                print strData,
            else:
                print strData
        if self.cnfConfiguration.blnLogging and not blnOmitLog and self.cnfConfiguration.blnNoteToLog:
            self.cnfConfiguration.flLogFile.write("%s%s%s%s" % (fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut), strData, fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendOut), (blnOmitNewline and [""] or ["\n"])[0]))
        if self.cnnConnection and not blnOmitRemote and self.cnfConfiguration.blnNoteToRemote:
            self.cnnConnection.fnSend(strData + (blnOmitNewline and [""] or ["\n"])[0])

    def fnLogDataOut(self, strData):
        """Log the outgoing data."""
        strData = fmFormatting.fnTrimNewline(strData)
        if self.cnfConfiguration.blnToScreen:
            print strData
        if self.cnfConfiguration.blnLogging:
            self.cnfConfiguration.flLogFile.write("%s%s%s\n" % (fmFormatting.fnExpandMacros(self.cnfConfiguration.strPrependOut), strData, fmFormatting.fnExpandMacros(self.cnfConfiguration.strAppendOut)))

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
        if self.cnfConfiguration.blnLogging:
            self.cnfConfiguration.flLogFile = file(fmFormatting.fnExpandMacros(self.cnfConfiguration.strLogFile), "a")
        if self.cnfConfiguration.strOnConnect != "":
            self.fnNoteData(fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnConnect) + "\n")
        self.fnSendData(self.cnfConfiguration.strName)
        self.cntConnection.push(self.cnfConfiguration.strPassword + "\n")
        if self.cnfConfiguration.strConnectionCommands != "":
            self.fnSendData(self.cnfConfiguration.strConnectionCommands)

    def OnDisconnect(self):
        """Clean up after disconnection."""
        if self.cnfConfiguration.strOnDisconnect != "":
            self.fnNoteData(fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnDisconnect) + "\n")
        if self.cnfConfiguration.blnLogging:
            self.cnfConfiguration.flLogFile.close()

    def OnRemoteConnect(self, strAddress):
        """Remote client connection callback."""
        if self.cnfConfiguration.strOnRemoteConnect != "":
            self.fnNoteData(fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnRemoteConnect, strAddress), blnOmitRemote = True)

    def OnRemoteDisconnect(self, strAddress):
        """Remote client disconnection callback."""
        if self.cnfConfiguration.strOnRemoteDisconnect != "":
            self.fnNoteData(fmFormatting.fnExpandMacros(self.cnfConfiguration.strOnRemoteDisconnect, strAddress), blnOmitRemote = True)

    def fnError(self, strDescription):
        print "ERROR: %s" % strDescription
        mdBot.fnExit()
        sys.exit()

    def fnException(self, strType, strValue, tbTraceback):
        print "Exception of type %s occurred in line %s, reason \"%s\"." % (strType, tbTraceback.tb_lineno, strValue)

class Callbacks:
    def __init__(self, plgNamespace):
        # Get the plugin reference so we can manipulate it.
        self.plgPlugin = plgNamespace
    def Send(self, strData):
        """Send data to the world."""
        mdBot.fnSendData(strData)
    def Note(self, strData):
        """Send text to stdout."""
        mdBot.fnNoteData(strData)
    def SetVariable(self, strVariableName, strData):
        """Set a variable in the plugin's variables dictionary."""
        self.plgPlugin.dicVariables[strVariableName] = strData
        return 0
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
            tmrTimer.blnEnabled = self.__getattr__(xmlTimer, "enabled", True)
            tmrTimer.strName = self.__getattr__(xmlTimer, "name")
            tmrTimer.strGroup = self.__getattr__(xmlTimer, "group")
            tmrTimer.strVariable = self.__getattr__(xmlTimer, "variable")
            tmrTimer.strScript = self.__getattr__(xmlTimer, "script")
            tmrTimer.intHour = int(self.__getattr__(xmlTimer, "hour"))
            tmrTimer.intMinute = int(self.__getattr__(xmlTimer, "minute"))
            tmrTimer.intSecond = int(self.__getattr__(xmlTimer, "second"))
            tmrTimer.intOffsetHour = int(self.__getattr__(xmlTimer, "offset_hour"))
            tmrTimer.intOffsetMinute = int(self.__getattr__(xmlTimer, "offset_minute"))
            tmrTimer.intOffsetSecond = int(self.__getattr__(xmlTimer, "offset_second"))
            tmrTimer.blnOneShot = self.__getattr__(xmlTimer, "one_shot", True)
            tmrTimer.blnOmitFromOutput = self.__getattr__(xmlTimer, "omit_from_output", True)
            tmrTimer.blnOmitFromLog = self.__getattr__(xmlTimer, "omit_from_log", True)
            tmrTimer.blnActiveClosed = self.__getattr__(xmlTimer, "active_closed", True)
            tmrTimer.blnAtTime = self.__getattr__(xmlTimer, "at_time", True)
            tmrTimer.strSend = xmlTimer.find("send").text
            tmrTimer.fltTime = time.time() + tmrTimer.intOffsetHour * 3600 + tmrTimer.intOffsetMinute * 60 + tmrTimer.intOffsetSecond

            if tmrTimer.intHour + tmrTimer.intMinute + tmrTimer.intSecond > 0:
                # Append it to the timers list.
                self.lstTimers.append(tmrTimer)
            else:
                mdBot.fnError("Timer has no interval set.")

    def loadtriggers(self, xmlTriggers):
        """Load triggers from the xmlTriggers node."""
        self.lstTriggers = []
        if xmlTriggers == None:
            return
        for xmlTrigger in xmlTriggers:
            trgTrigger = Plugin.Trigger()
            trgTrigger.blnEnabled = self.__getattr__(xmlTrigger, "enabled", True)
            trgTrigger.blnKeepANSI = self.__getattr__(xmlTrigger, "keep_ansi", True)
            trgTrigger.strName = self.__getattr__(xmlTrigger, "name")
            trgTrigger.strGroup = self.__getattr__(xmlTrigger, "group")
            trgTrigger.blnIgnoreCase = self.__getattr__(xmlTrigger, "ignore_case", True)
            trgTrigger.blnRegexp = self.__getattr__(xmlTrigger, "regexp", True)
            trgTrigger.blnKeepEvaluating = self.__getattr__(xmlTrigger, "keep_evaluating", True)
            trgTrigger.strMatch = self.__getattr__(xmlTrigger, "match")
            trgTrigger.intSequence = int(self.__getattr__(xmlTrigger, "sequence"))
            trgTrigger.intSendTo = int(self.__getattr__(xmlTrigger, "send_to"))
            trgTrigger.strScript = self.__getattr__(xmlTrigger, "script")

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

    def run(self, strFunctionName, tplArguments):
        """Execute the function in the plugin namespace."""
        try:
            self.dicGlobals[strFunctionName](*tplArguments)
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
        self.strName = self.__getattr__(xmlPlugin, "name")
        self.strID = self.__getattr__(xmlPlugin, "id")
        self.blnSaveState = self.__getattr__(xmlPlugin, "save_state", True)
        if self.__getattr__(xmlPlugin, "language").lower() != "python":
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

    def __selyn__(self, strText):
        """Convert 'y'/'n' to True or False."""
        if strText.lower() == "y":
            return True
        else:
            return False

    def __getattr__(self, xmlNode, strAttribute, blnYN = False):
        """Get the value of strAttribute from xmlNode, converting it to
           binary if blnYN is True."""

        dicAttributes = {
            "appendin": "",               # Text to append to incoming data.
            "appendout": "",              # Text to append to outgoing data.
            "active_closed": False,       # Is timer active when the world is closed? (NS)
            "at_time": False,             # At time for timer. (NS)
            "back_colour": None,          # Backcolour to match on. (NS)
            "bold": None,                 # Match if the text is bold. (NS)
            "connectioncommands": "",     # Commands to send on connection.
            "debug": False,               # Print debugging data in the output.
            "enabled": False,             # Is the item enabled?
            "expand_variables": False,    # Expand variables. (NS)
            "group": "",                  # Item group name.
            "hour": 0,                    # Hour interval for timers. (NS)
            "ignore_case": False,         # Ignore case. (NS)
            "inverse": None,              # Match if the text is inverse. (NS)
            "italic": None,               # Match if the text is italic. (NS)
            "keep_ansi": False,           # Keep the ANSI codes to match on.
            "keep_evaluating": True,      # Keep evaluating after a trigger has been matched.
            "localport": 4000,            # Port number to listen to.
            "logfile": "log.txt",         # Log filename.
            "logging": False,             # Is logging enabled?
            "match_back_colour": False,   # Enable match on backcolour. (NS)
            "match_bold": False,          # Enable match on bold. (NS)
            "match_inverse": False,       # Enable match on inverse. (NS)
            "match_italic": False,        # Enable match on italic. (NS)
            "match_text_colour": False,   # Enable match on forecolour. (NS)
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
            "text_colour": None,          # Forecolour to match on. (NS)
            "variable": "",               # Variable to send to (NS)
        }
        if strAttribute in xmlNode.attrib:
            if blnYN:
                return self.__selyn__(xmlNode.attrib[strAttribute])
            else:
                return xmlNode.attrib[strAttribute]
        else:
            if strAttribute in dicAttributes:
                return dicAttributes[strAttribute]
            else:
                mdBot.fnError("Mandatory attribute \"%s\" does not exist." % strAttribute)


class Configuration:
    def __init__(self, strFilename="character.xml"):
        """Load the configuration from the specified file."""
        try:
            flFile = file(strFilename)
        except:
            print("Cannot open file '%s' for reading." % strFilename)
            sys.exit()
        xmlTree = ET.parse(flFile)
        xmlRoot = xmlTree.getroot()
        # Create this here so we can access the __getattr__ function.
        self.dicPlugins = {"000000000000000000000000": Plugin()}
        plgNamespace = self.dicPlugins["000000000000000000000000"]

        # Connection details.
        self.strHost = plgNamespace.__getattr__(xmlRoot, "host")
        self.intPort = int(plgNamespace.__getattr__(xmlRoot, "port"))
        self.intLocalPort = int(plgNamespace.__getattr__(xmlRoot, "localport"))
        self.strName = plgNamespace.__getattr__(xmlRoot, "name")
        self.strPassword = base64.decodestring(plgNamespace.__getattr__(xmlRoot, "password"))
        self.strConnectionCommands = plgNamespace.__getattr__(xmlRoot, "connectioncommands").decode("string_escape")

        # Debugging.
        self.blnDebug = plgNamespace.__getattr__(xmlRoot, "debug", True)
        self.blnNoteToConsole = plgNamespace.__getattr__(xmlRoot, "notetoconsole", True)
        self.blnNoteToLog = plgNamespace.__getattr__(xmlRoot, "notetolog", True)
        self.blnNoteToRemote = plgNamespace.__getattr__(xmlRoot, "notetoremote", True)

        # Logging.
        xmlLogging = xmlRoot.find("logging")
        self.blnLogging = plgNamespace.__getattr__(xmlLogging, "enabled", True)
        self.blnToScreen = plgNamespace.__getattr__(xmlLogging, "toscreen", True)
        self.blnKeepANSI = plgNamespace.__getattr__(xmlLogging, "keep_ansi", True)
        self.strLogFile = plgNamespace.__getattr__(xmlLogging, "logfile")
        self.strAppendIn = plgNamespace.__getattr__(xmlLogging, "appendin").decode("string_escape")
        self.strAppendOut = plgNamespace.__getattr__(xmlLogging, "appendout").decode("string_escape")
        self.strPrependIn = plgNamespace.__getattr__(xmlLogging, "prependin").decode("string_escape")
        self.strPrependOut = plgNamespace.__getattr__(xmlLogging, "prependout").decode("string_escape")
        self.strOnConnect = plgNamespace.__getattr__(xmlLogging, "onconnect").decode("string_escape")
        self.strOnDisconnect = plgNamespace.__getattr__(xmlLogging, "ondisconnect").decode("string_escape")
        self.strOnRemoteConnect = plgNamespace.__getattr__(xmlLogging, "onremoteconnect").decode("string_escape")
        self.strOnRemoteDisconnect = plgNamespace.__getattr__(xmlLogging, "onremotedisconnect").decode("string_escape")

        # Pass the standard namespace to the plugin so we can access it.
        plgNamespace.strID = "000000000000000000000000"
        plgNamespace.strName = "Main namespace"
        plgNamespace.blnSaveState = True
        plgNamespace.dicVariables = {}
        plgNamespace.strScript = ""

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

class MUDConnection(asynchat.async_chat):
    def __init__(self, strHost, intPort):
        asynchat.async_chat.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_terminator("\n")
        self.strBuffer = ""
        try:
            self.connect((strHost, intPort))
        except:
            self.close()
            mdBot.fnError("Could not connect to host, reason is '%s'." % sys.exc_value[1])
    def handle_connect(self):
        mdBot.OnConnect()
    def handle_close(self):
        mdBot.OnDisconnect()
        if mdBot.cnnConnection != None:
            mdBot.cnnConnection.close()
        self.close()
        mdBot.fnExit()
    def collect_incoming_data(self, data):
        strData = data
        if mdBot.cnnConnection != None:
            mdBot.cnnConnection.fnSend(strData)
        self.strBuffer = self.strBuffer + strData
    def found_terminator(self):
        if mdBot.cnnConnection != None:
            mdBot.cnnConnection.fnSend("\n")
        mdBot.fnProcessData(self.strBuffer.replace("\r", ""))
        self.strBuffer = ""

class ClientDispatcher(asynchat.async_chat):
    def __init__(self, (cnnConnection, strAddress)):
        asynchat.async_chat.__init__(self, cnnConnection)
        self.set_terminator("\n")
        self.strBuffer = ""
        self.strAddress = strAddress[0]
        self.intState = 0 # Name auth
        self.push("You have connected to MUDdrop. Please enter the character name: \n")
    def handle_close(self):
        global server
        mdBot.OnRemoteDisconnect(self.strAddress)
        server = Server()
        mdBot.cnnConnection = None
        self.close()
    def collect_incoming_data(self, data):
        strData = data
        self.strBuffer = self.strBuffer + strData
    def found_terminator(self):
        if self.intState == 0:
            self.strAuthName = self.strBuffer.strip().lower()
            self.intState = 1
            self.push("Please enter the character password: \n")
        elif self.intState == 1:
            if self.strAuthName == mdBot.cnfConfiguration.strName.lower() and self.strBuffer.strip() == mdBot.cnfConfiguration.strPassword:
                self.intState = 2
                self.push("Welcome to %s.\n" % mdBot.cnfConfiguration.strName)
                mdBot.OnRemoteConnect(self.strAddress)
            else:
                self.push("Wrong name/password.\n")
                self.handle_close()
        elif self.intState == 2:
            mdBot.cntConnection.send(self.strBuffer)
            mdBot.fnLogDataOut(self.strBuffer)
        self.strBuffer = ""
    def fnSend(self, strData):
        """Sends the data if the user has authenticated."""
        if self.intState == 2: # Authenticated.
            self.push(strData)

class Server(asyncore.dispatcher):
    def __init__(self):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind(('', mdBot.cnfConfiguration.intLocalPort))
        self.listen(1)

    def handle_accept(self):
        mdBot.cnnConnection = ClientDispatcher(self.accept())
        self.close()

tmrThreadTimer = None
fmFormatting = Formatting()
mdBot = MUDdrop()
mdBot.init()
mdBot.cntConnection = MUDConnection(mdBot.cnfConfiguration.strHost, mdBot.cnfConfiguration.intPort)
server = Server()
asyncore.loop()
