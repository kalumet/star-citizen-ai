import json
from enum import Enum
import time
from difflib import SequenceMatcher
from elevenlabs import generate, stream, Voice, voices
from wingmen.open_ai_wingman import OpenAiWingman
from wingmen.star_citizen_services.keybindings import SCKeybindings
from wingmen.star_citizen_services.uex_api import UEXApi
from wingmen.star_citizen_services.screenshot_ocr import TransportMissionAnalyzer
from services.printr import Printr
from services.secret_keeper import SecretKeeper


DEBUG = False


def print_debug(to_print):
    if DEBUG:
        print_debug(to_print)


printr = Printr()
try:
    import pydirectinput as key_module
except AttributeError:
    # TODO: Instead of creating a banner make this an icon in the header
    # printr.print_warn(
    #     "pydirectinput is only supported on Windows. Falling back to pyautogui which might not work in games.",
    #     wait_for_gui=True
    # )
    import pyautogui as key_module


class StarCitizenWingman(OpenAiWingman):
    """This Wingman uses the OpenAiWingman as basis, but has some major differences:

    1. It has Multi-Wingman Capabilities
    - Execute instant Actions as of OpenAiWingman using the standard configuration -> Great for this!
    - Execute command chains as of OpenAiWingman (combining a series of key presses) -> Great for this!
    
    Big differences starts now:
    - Execute star-citizen commands as specified by your current keybinding-settings for a given release. All it needs is the default keybinding file + your exported keybinds overwrites. No need to config any commands but the instant-commands and the complex key changes
    - incorporates UEX trading API letting you ask for best trade route (1 stop) between locations, best trade route from a given location, best sell option for a commodity, best selling option for a commodity at a location
    - TODO incorporates Galactepedia-Wiki search to get more detailed information about the game world and lore in game
    - TODO incorporates Delivery Mission Management: based on delivery missionS (yes multiple) you are interested in, it will analyse the mission text, extract the payout, the locations from where you have to retrieve a package and the associated delivery location, calculates the best collection and delivery route to have to travel the least possible stops + incorporates profitable trade routes between the stops to earn an extra money on the trip.
    - TODO based on a given trading consoles it will extract the location, commodity prices and stock levels and update the prices automatically in the UEX database
    
    2. smart Action selection with reduced context size
    - The system will get your command and will first ask GPT what kind of Action the user wants to execute (one of the above)
    - The response will be used, to make another call to GPT now with the appropriate context information (commands for keypresses, trading calls, wiki calls, box mission calls...)

    3. TODO Context-Logic cache: depending on your tasks, it will have different conversation-caches that will be selected on demand. Examples
    - If you execute a simple command, there is no need for a history, every gpt call can be fresh, effectively reducing cost of api usage. 
    - If you do a box mission, it will keep the history until you have completed your box mission(s), beeing able to guide you from box to box
    - If you do a trade, it will keep the history until it understands that you start your trading route from a complete different location.
    
    3. TODO Cache-Logic for text-to-speech responses to reduce cost with config parameters to specify the ammount of responses to be cached vs. dropped vs. renewed
    """

    class AIContext(Enum):
        """
        used to switch between contexts of my ai
        """
        CORA = "CoraInteractionRequests"
        TDD = "TradeAndDevelopmentDivisionRequests"

    def __init__(self, name: str, config: dict[str, any], secret_keeper: SecretKeeper):
        super().__init__(name, config, secret_keeper)
        
        self.contexts_history = {}  # Dictionary to store the state of different conversion contexts
        self.current_context: self.AIContext = self.AIContext.CORA  # Name of the current context
        self.current_user_request: str = None  # saves the current user request
        self.switch_context_executed = False  # used to identify multiple switch executions that would be incorrect -> Error
        self.sc_keybinding_service: SCKeybindings = SCKeybindings(config)
        self.sc_keybinding_service.parse_and_create_files()
        self.uex_service = None  # set in validate()
        self.tdd_voice = "onyx"
        self.config["openai"]["tts_voice"] = self.config["openai"]["contexts"]["cora_voice"]
        self.config["features"]["play_beep_on_receiving"] = False
        self.config["features"]["enable_radio_sound_effect"] = False
        self.config["features"]["enable_robot_sound_effect"] = True
        self.config["openai"]["conversation_model"] = self.config["openai"]["contexts"][f"context-{self.AIContext.CORA.name}"]["conversation_model"]

        # every conversation starts with the "context" that the user has configured
        self._set_current_context(self.AIContext.CORA, new=True)
        
        # init the configuration dynamically based on current star citizen settings.
        # the config is dynamically expanded for all mapped keybinds.
        # the user only configures special commands i.e. combination of keybindings to press

    def validate(self):
        errors = super().validate()
        uex_api_key = self.secret_keeper.retrieve(
            requester=self.name,
            key="uex",
            friendly_key_name="UEX API key",
            prompt_if_missing=True,
        )
        if not uex_api_key:
            errors.append(
                "Missing 'uex' API key. Please provide a valid key in the settings."
            )
        else:
            self.uex_service = UEXApi.init(uex_api_key)

    def _set_current_context(self, new_context: AIContext, new: bool = False):
        """Set the current context to the specified context name."""
        self.current_context = new_context

        if new:
            # initialise a new context conversation history
            self.current_tools = self._get_context_tools(current_context=new_context)
            self.current_context = new_context
            context_prompt = f'{self.config["openai"]["contexts"].get(f"context-{new_context.name}")}'
            context_switch_prompt = ""
            if new_context == self.AIContext.CORA:
                context_prompt += f' The character you are supporting is named "{self.config["openai"]["player_name"]}". His title is {self.config["openai"]["player_title"]}. His ship call id is {self.config["openai"]["ship_name"]}. He wants you to respond in {self.config["sc-keybind-mappings"]["player_language"]}'
                context_switch_prompt += " If the current user request does not fit the current context, switch to an appropriate context by calling the switch_context function. Do not switch, if the player adresses you by 'Cora'. Only switch context, if the request is trading related."

            if new_context == self.AIContext.TDD:
                context_switch_prompt += " Whenever the player adresses a new Trading Devision Location, switch the employee by calling the function switch_tdd_employee and select a different employee_id. If the current user request does not fit the current context, switch to an appropriate context by calling the switch_context function. Do switch, if the requests adresses 'Cora' or using words like 'computer' or demanding an action."

            self.messages = [{"role": "system", "content": f'{context_prompt}. On a request of the Player you will identify the context of his request. Valid contexts are: Player or Ship Actions executed by the AI companion ({self.AIContext.CORA.value}), for all requests related to player actions to be executed for the player. Trade and Development Division ({self.AIContext.TDD.value}) personel, for all requests related to Trading. The current context is: {new_context.value}. {context_switch_prompt}'}]
        else:
            # get the saved context history
            tmp_new_context_history = self.contexts_history[new_context]
            self.current_tools = tmp_new_context_history.get("tools").copy()
            self.messages = tmp_new_context_history.get("messages").copy()
            self.current_context = new_context

        if new_context == self.AIContext.CORA:
            self.messages_buffer = 10 # making player Actions does not require much historical buffer
        elif new_context == self.AIContext.TDD:
            self.messages_buffer = 20 # dealing on the journey of the player to trade might require more context-information in the conversation
        
    def _save_current_context(self):
        """Save the current conversation history under the current context."""
        if self.current_context is not None:
            self.contexts_history[self.current_context] = { "messages": self.messages.copy(),
                                                            "tools": self.current_tools
                                                        }

    def _switch_context(self, new_context_name):    
        """Switch to a different context, saving the current one."""
        try:
            # Versuche, den Kontext basierend auf dem Namen zu finden
            context_to_switch_to = self.AIContext(new_context_name)
            # Überprüfe, ob der Kontext im Dictionary existiert
            if context_to_switch_to in self.contexts_history:
                # Logik für den Fall, dass der Kontext existiert
                printr.print(f"Lade context {context_to_switch_to}.", tags="info")
                self._save_current_context()
                self._set_current_context(new_context=context_to_switch_to)
                
            else:
                # Logik für den Fall, dass der Kontext nicht existiert
                printr.print(f"Erstelle context {context_to_switch_to}.", tags="info")
                self._save_current_context()
                self._set_current_context(new_context=context_to_switch_to, new=True)

        except AttributeError:
            # Fehlerbehandlung, falls der Kontextname nicht existiert
            print_debug(f"Kontext {new_context_name} ist kein gültiger Kontext.")

    def _get_context_tools(self, current_context: AIContext):
        if current_context == self.AIContext.CORA:
            return self._get_cora_tools()
        elif current_context == self.AIContext.TDD:
            return self._get_tdd_tools()
        else:
            return self._context_switch_tool()

    # transcription stays as specified in OpenAIWingman
    # async def _transcribe(self, audio_input_wav: str) -> tuple[str | None, str | None]:
       
    async def _get_response_for_transcript(
        self, transcript: str, locale: str | None
    ) -> tuple[str, str]:
        """Overwritten to deal with dynamic context switches.
        Gets the response for a given transcript.

        This function interprets the transcript, runs instant commands if triggered,
        calls the OpenAI API when needed, processes any tool calls, and generates the final response.
        

        Args:
            transcript (str): The user's spoken text transcribed.

        Returns:
            A tuple of strings representing the response to a function call and an instant response.
        """
        self.last_transcript_locale = locale
        self.current_user_request = transcript

        instant_response = self._execute_instant_activation_command(transcript)
        if instant_response:
            return instant_response, instant_response
        if instant_response is False:
            return False, False

        self._add_user_message(transcript)
        response_message, tool_calls = self._make_gpt_call()

        if tool_calls:
            instant_response = await self._handle_tool_calls(tool_calls)
            
            # if self.switch_context_executed:
            #     instant_response = None
            #     response_message = None
            #     tool_calls = None
            #     # repeat the gpt call now in the new switched context
            #     print_debug("switched context")
            #     # msg = {"role": "user", "content": transcript}
            #     # self.messages.append(msg)
            #     # response_message, tool_calls = self._make_gpt_call()
            #     self.switch_context_executed = False # might be problematik, if gpt tries to make another context switch.
            #     # if tool_calls:
            #     #     instant_response = await self._handle_tool_calls(tool_calls)

            # if instant_response: # might be "Error"
            #     return instant_response, instant_response

            summarize_response = self._summarize_function_calls()
            return self._finalize_response(str(summarize_response))

        # print_debug(self.messages[1:])

        return response_message.content, response_message.content

    def _make_gpt_call(self):
        completion = self._gpt_call()

        if completion is None:
            return None, None

        response_message, tool_calls = self._process_completion(completion)

        # do not tamper with this message as it will lead to 400 errors!
        self.messages.append(response_message)
        return response_message, tool_calls

    # def _add_user_message(self, content: str):

    def _cleanup_conversation_history(self):
        """Cleans up the conversation history by removing messages that are too old.
        Overwritten with context switch sensitive message buffers"""
        remember_messages = self.messages_buffer

        if remember_messages is None:
            return

        # Calculate the max number of messages to keep including the initial system message
        # `remember_messages * 2` pairs plus one system message.
        max_messages = (remember_messages * 2) + 1

        # every "AI interaction" is a pair of 2 messages: "user" and "assistant" or "tools"
        deleted_pairs = 0

        while len(self.messages) > max_messages:
            if remember_messages == 0:
                # Calculate pairs to be deleted, excluding the system message.
                deleted_pairs += (len(self.messages) - 1) // 2
                self.reset_conversation_history()
            else:
                while len(self.messages) > max_messages:
                    del self.messages[1:3]
                    deleted_pairs += 1

        if self.debug and deleted_pairs > 0:
            printr.print(
                f"   Deleted {deleted_pairs} pairs of messages from the conversation history.",
                tags="warn",
            )

    # def _remove_messages_from_conversation(self, number_messages_to_remove):

    # def reset_conversation_history(self):

    # def _try_instant_activation(self, transcript: str) -> str:

    # def _execute_command(self, command: dict) -> str:

    def _gpt_call(self): 
        """Overwrites to select the correct tools for the current context

        Returns:
            The GPT completion object or None if the call fails.
        """
        if self.debug:
            printr.print(
                f"   Calling GPT with {(len(self.messages) - 1) // 2} message pairs (excluding context)",
                tags="info",
            )
        return self.openai.ask(
            messages=self.messages,
            tools=self.current_tools,
            model=self.config["openai"].get("conversation_model")  # TODO make context sensitive model selection
        )

    # def _process_completion(self, completion):

    async def _handle_tool_calls(self, tool_calls):
        """Processes all the tool calls identified in the response message.
        Overwritten to handle context switches

        Args:
            tool_calls: The list of tool calls to process.

        Returns:
            str: The immediate response from processed tool calls or None if there are no immediate responses.
        """
        instant_response = None
        function_response = ""

        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)
            (
                function_response,
                instant_response,
            ) = await self._execute_command_by_function_call(
                function_name, function_args
            )

            msg = {"role": "tool", "content": function_response}
            if tool_call.id is not None:
                msg["tool_call_id"] = tool_call.id
            if function_name is not None:
                msg["name"] = function_name

            # Don't use self._add_user_message_to_history here because we never want to skip this because of history limitions
            self.messages.append(msg)

        return instant_response

    async def _play_to_user(self, text: str):
        """Plays audio to the user using the configured TTS Provider (default: OpenAI TTS).
        Also adds sound effects if enabled in the configuration.

        Args:
            text (str): The text to play as audio.
        """
        if not text or text == "Ok":
            return
        
        if self.tts_provider == "edge_tts":
            edge_config = self.config["edge_tts"]
            tts_voice = edge_config.get("tts_voice")
            gender = edge_config.get("gender")
            detect_language = edge_config.get("detect_language")

            if detect_language:
                tts_voice = await self.edge_tts.get_same_random_voice_for_language(
                    gender, self.last_transcript_locale
                )

            await self.edge_tts.generate_speech(
                text, filename="audio_output/edge_tts.mp3", voice=tts_voice
            )

            if self.config.get("features", {}).get("enable_robot_sound_effect"):
                self.audio_player.effect_audio("audio_output/edge_tts.mp3")

            self.audio_player.play("audio_output/edge_tts.mp3")
        elif self.tts_provider == "elevenlabs":
            elevenlabs_config = self.config["elevenlabs"]
            voice = elevenlabs_config.get("voice")
            if not isinstance(voice, str):
                voice = Voice(voice_id=voice.get("id"))
            else:
                voice = next((v for v in voices() if v.name == voice), None)

            voice_setting = self._get_elevenlabs_settings(elevenlabs_config)
            if voice_setting:
                voice.settings = voice_setting

            response = generate(
                text,
                voice=voice,
                model=elevenlabs_config.get("model"),
                stream=True,
                api_key=self.elevenlabs_api_key,
                latency=elevenlabs_config.get("latency", 3),
            )
            stream(response)
        else:  # OpenAI TTS
            response = self.openai.speak(text, self.config["openai"].get("tts_voice"))
            if response is not None:
                self.audio_player.stream_with_effects(
                    response.content,
                    self.config.get("features", {}).get("play_beep_on_receiving"),
                    self.config.get("features", {}).get("enable_radio_sound_effect"),
                    self.config.get("features", {}).get("enable_robot_sound_effect"),
                )

    async def _execute_command_by_function_call(
        self, function_name: str, function_args: dict[str, any]
    ) -> tuple[str, str]:
        """
        Uses an OpenAI function call to execute a command. If it's an instant activation_command, one if its reponses will be played.

        Args:
            function_name (str): The name of the function to be executed.
            function_args (dict[str, any]): The arguments to pass to the function being executed.

        Returns:
            A tuple containing two elements:
            - function_response (str): The text response or result obtained after executing the function.
            - instant_response (str): An immediate response or action to be taken, if any (e.g., play audio).
        """
        
        function_response = ""
        instant_reponse = ""
        # our context switcher. If this is called by GPT, we switch to this context (with its own memory).
        # we have to repeat the user request on this switched context, to get a valid response.
        if function_name == "switch_context":
            return self._execute_switch_context_function(function_args)
        
        if function_name == "execute_command":
            # get the command from config file based on the argument passed by GPT
            command = self._get_command(function_args["command_name"])
            # execute the command
            if command:
                function_response = self._execute_star_citizen_command_overwrite(command)
            else:
                function_response, instant_reponse = self._execute_star_citizen_keymapping_command(function_args["command_name"])

            # if the command has responses, we have to play one of them
            if command and command.get("responses"):
                instant_reponse = self._select_command_response(command)
                await self._play_to_user(instant_reponse)

        if function_name == "switch_tdd_employee":
            self.tdd_voice = function_args["employee_id"]
            self.config["openai"]["tts_voice"] = self.tdd_voice
            printr.print(f'Neuer TDD Ansprechpartner: {function_args["employee_id"]}', tags="info")
            return json.dumps({"success":"True"}), None
        
        if function_name == "find_best_trade_route_from_location":
            function_response = json.dumps(self.uex_service.find_best_trade_from_location(location_name=function_args["location_name"]))
            printr.print(f'-> Resultat: {function_response}', tags="info") 

        if function_name == "find_best_trade_route_between_locations":
            function_response = json.dumps(self.uex_service.find_best_trade_between_locations(location_name1=function_args["location_name_from"], location_name2=function_args["location_name_to"]))
            printr.print(f'-> Resultat: {function_response}', tags="info")

        if function_name == "find_best_sell_price_for_commodity_at_location":
            function_response = json.dumps(self.uex_service.find_best_sell_price_at_location(location_name=function_args["location_name_to"], commodity_name=function_args["commodity_name"]))
            printr.print(f'-> Resultat: {function_response}', tags="info")

        if function_name == "find_best_trade_route_for_commodity":
            function_response = json.dumps(self.uex_service.find_best_trade_for_commodity(commodity_name=function_args["commodity_name"]))
            printr.print(f'-> Resultat: {function_response}', tags="info")

        if function_name == "find_best_selling_location_for_commodity":
            function_response = json.dumps(self.uex_service.find_best_selling_location_for_commodity(commodity_name=function_args["commodity_name"]))
            printr.print(f'-> Resultat: {function_response}', tags="info")


        return function_response, instant_reponse

    def _execute_switch_context_function(self, function_args):
        print_debug(f'switching context call: {function_args["context_name"]}')
        # if self.switch_context_executed is True:
        #     # ups, there has already been a context switch before. We do avoid infinite loops here...
        #     printr.print(
        #         "   GPT trying to make multiple context switches, this is not allowed.",
        #         tags="warn",
        #     )
        #     self.switch_context_executed = False
        #     return "Error", None
        # first, we have to remove from the current context old information from the "old" request history, as it belongs to the context that we switch to
        context_messages = None
        if len(self.messages) >= 3:  # bis hierhin wurde hinzugefügt: benutzerrequest und gpt response (tool_call: switch!). Mit der system message,  müssen also mindestens 3 nachrichten vorhanden sein
            # wir entfernen die neuesten 2 nachrichten: user request + tool_call
            context_messages = self.messages[-2:]
            del self.messages[-2:]

            # get the command based on the argument passed by GPT
        context_name_to_switch_to = function_args["context_name"]
        self._switch_context(context_name_to_switch_to)
        if self.current_context == self.AIContext.CORA:
            self.config["openai"]["tts_voice"] = self.config["openai"]["contexts"]["cora_voice"]
            self.config["features"]["play_beep_on_receiving"] = False
            self.config["features"]["enable_radio_sound_effect"] = False
            self.config["features"]["enable_robot_sound_effect"] = True
            self.config["openai"]["conversation_model"] = self.config["openai"]["contexts"][f"context-{self.AIContext.CORA.name}"]["conversation_model"]
        elif self.current_context == self.AIContext.TDD:
            self.config["openai"]["tts_voice"] = self.tdd_voice
            self.config["features"]["play_beep_on_receiving"] = True
            self.config["features"]["enable_radio_sound_effect"] = True
            self.config["features"]["enable_robot_sound_effect"] = False
            self.config["openai"]["conversation_model"] = self.config["openai"]["contexts"][f"context-{self.AIContext.TDD.name}"]["conversation_model"]
        self.messages.extend(context_messages)  # we readd the messages to the switched context.
        # self._add_user_message(self.current_user_request) # we readd the user message to the new context to make the same user request in the new context
        # context has been switched, so we can return the contexts swtich request
        # self.switch_context_executed = True
        function_response = f"switched to context {context_name_to_switch_to}, reevaluate the user request, keep the users language"
        instant_reponse = None
        return function_response, instant_reponse

    def _execute_star_citizen_keymapping_command(self, command_name: str):
        """This method will execute a keystroke as defined in the keybinding settings of the game"""
        command = self.sc_keybinding_service.get_command(command_name)

        if not command:
            print_debug(f"Command not found {command_name}")
            return json.dumps({"success": False, "error": f"Command not found {command_name}"}), f"Command not found {command_name}"

        avoid_filter_names = self.config.get("avoid-commands", [])
        avoid_filter_names_set = set(avoid_filter_names)
        if command_name in avoid_filter_names_set:
            print_debug(f"Command not allowed {command_name}")
            return json.dumps({"success": False, "error": f"Command not allowed {command_name}"}), f"Command not allowed {command_name}"

        # Definiere eine Reihenfolge für die Modifiertasten
        order = ["alt", "ctrl", "shift", "altleft", "ctrlleft", "shiftleft", "altright", "ctrlrigth", "shiftright"]
        modifiers = set(order)
        keys = command.get("keyboard-mapping").split("+")
        
        actionname = command.get("actionname")
        category = command.get("category")

        sc_activation_mode = command.get("activationMode")
        
        # we check, if the command is overwritten in the config
        overwrite_commands = self.config["sc-keybind-mappings"].get("overwrite_sc_command_execution",{})
        overwrite_command = overwrite_commands.get(command_name)
        if overwrite_command and overwrite_command.get("hold"):
            old_sc_activation_mode = sc_activation_mode
            sc_activation_mode = overwrite_command.get("hold")
            print_debug(f"overwritten activationMode: was {old_sc_activation_mode} -> {sc_activation_mode}")

        hold = self.config["sc-keybind-mappings"]["key-press-mappings"].get(sc_activation_mode)

        command_desc = None
        if not command.get("action-description-en"):
            if not command.get("action-label-en"):
                command_desc = actionname
            else:
                command_desc = command.get("action-label-en")
        else:
            command_desc = command.get("action-description-en")
        
        command_message = f'{command_desc}: {command.get("keyboard-mapping-en")}'
        
        printr.print(
                f"   Executing command: {command_message}",
                tags="info",
            )

        try: 
            if len(keys) > 1:
                keys = sorted(keys, key=lambda x: order.index(x) if x in order else len(order))
            elif hold == "double_tap":  # double tab is only one key always
                print_debug(f"double tab {keys[0]}")
                key_module.press(keys[0])
                time.sleep(0.050)
                key_module.press(keys[0])
                return "Ok", "Ok"
        
            if hold == "notSupported":
                return json.dumps({"success": "False", "message": f"{command_message}", "error": f"activation mode not supported {sc_activation_mode}"}), None

            active_modifiers = []

            for sc_key in keys:
                key = self.config["sc-keybind-mappings"]["key-mappings"].get(sc_key)  # not all star citizen key names are  equal to key_modul names, therefore we do a mapping
                if not key:  # if there is no mapping, key names are identical
                    key = sc_key

                if key in modifiers:
                    key_module.keyDown(key)
                    print_debug("modifier down: " + key)
                    active_modifiers.append(key)
                    continue

                if hold == "unknown":
                    print_debug("unknown activationMode assuming press: " + key)
                    key_module.press(key)

                if hold > 1:
                    print_debug("hold and release: " + key)
                    key_module.keyDown(key)  # apart of modifiers, we assume, that there is always only one further key that can be pressed ....
                    time.sleep(hold / 1000)  # transform in seconds
                    key_module.keyUp(key)
                else:
                    print_debug("press: " + key)
                    key_module.press(key)
            
            active_modifiers.reverse()  # Kehrt die Liste in-place um

            for modifier_key in active_modifiers:  # release modifiers in inverse order
                print_debug("modifier up: " + modifier_key)
                key_module.keyUp(modifier_key) 

            return "Ok", "Ok"
        except Exception as e:
            # Genereller Fehlerfang
            print_debug(f"Ein Fehler ist aufgetreten: {e.__class__.__name__}, {e}")
            return json.dumps({"success": "False", "error": f"{e}"}), None
    
    def _execute_instant_activation_command(self, transcript: str) -> dict | None:
        """Overwrites wingman.py: Uses a fuzzy string matching algorithm to match the transcript to a configured instant_activation command and executes it immediately.

        Args:
            transcript (text): What the user said, transcripted to text. Needs to be similar to one of the defined instant_activation phrases to work.

        Returns:
            {} | None: The executed instant_activation command.
        """
    
        TransportMissionAnalyzer.testScreenshot()
        
        instant_activation_commands = [
            command
            for command in self.config.get("commands", [])
            if command.get("instant_activation")
        ]

        best_command = None
        best_ratio = 0
        # check if transcript matches any instant activation command. Each command has a list of possible phrases
        for command in instant_activation_commands:
            for phrase in command.get("instant_activation"):
                ratio = SequenceMatcher(
                    None,
                    transcript.lower(),
                    phrase.lower(),
                ).ratio()
                if ratio > 0.8:  # we only accept commands that have a high ration
                    if ratio > best_ratio: # some command activations might have quite similar values, therefore, we need to check if there are better commands!
                        best_command = command
                        best_ratio = ratio
        
        self._execute_star_citizen_command_overwrite(best_command)

        if best_command:
            if best_command.get("responses") is False:
                return "Ok"
            
            if len(best_command.get("responses", [])) == 0:
                return None  # gpt decides how the response should be
            return self._select_command_response(best_command)  # we want gpt to decide how the response should be
        return None
      
    def _execute_star_citizen_command_overwrite(self, command: dict):
        """Triggers the execution of a command. This implementation executes the defined star citizen commands.

        Args:
            command (dict): The command object from the config to execute

        Returns:
            str: the selected response from the command's responses list in the config. "Ok" if there are none.
        """
        if not command:
            return "Command not found"

        printr.print(f"-> Executing sc-command: {command.get('name')}", tags="info")

        if self.debug:
            printr.print(
                "Skipping actual keypress execution in debug_mode...", tags="warn"
            )

        response = None
        if len(command.get("sc_commands", [])) > 0 and not self.debug:
            for sc_command in command.get("sc_commands"):
                self._execute_star_citizen_keymapping_command(sc_command["sc_command"])
                if sc_command.get("wait"):
                    time.sleep(sc_command["wait"])
            if command.get("responses") is not False:
                response = self._select_command_response(command)

        if len(command.get("sc_commands", [])) == 0:  # is a default configuration
            return self._execute_command(command)

        if not response:
            response = json.dumps({"success": True, "instruction": "Don't make any function call!"})
        return response

    # def _build_tools(self) -> list[dict]:
    
    def _execute_command(self, command: dict) -> str:
        """needs overwrite. no changes. 
        
        Triggers the execution of a command. This base implementation executes the keypresses defined in the command.

        Args:
            command (dict): The command object from the config to execute

        Returns:
            str: the selected response from the command's responses list in the config. "Ok" if there are none.
        """

        if not command:
            return "Command not found"

        if self.debug:
            printr.print(
                "Skipping actual keypress execution in debug_mode...", tags="warn"
            )

        if len(command.get("keys", [])) > 0 and not self.debug:
            self.execute_keypress(command)
        # TODO: we could do mouse_events here, too...

        # handle the global special commands:
        if command.get("name", None) == "ResetConversationHistory":
            self.reset_conversation_history()

        if not self.debug:
            # in debug mode we already printed the separate execution times
            self.print_execution_time()

        return self._select_command_response(command) or "Ok"
    
    def execute_keypress(self, command: dict):
        """Executes the keypresses defined in the command in order.

        pydirectinput uses SIGEVENTS to send keypresses to the OS. This lib seems to be the only way to send keypresses to games reliably.

        It only works on Windows. For MacOS, we fall back to PyAutoGUI (which has the exact same API as pydirectinput is built on top of it).

        Args:
            command (dict): The command object from the config to execute
        """

        modifier_order = ["alt", "ctrl", "shift", "altleft", "ctrlleft", "shiftleft", "altright", "ctrlrigth", "shiftright"]
        keys = command.get("keys", [])
        
        active_modifiers = []

        for entry in keys:
            print_debug(entry)
            key = entry["key"]
            if entry.get("modifier"):
                print_debug(f'down {entry.get("modifier")}')
                key_module.keyDown(entry.get("modifier"))
            
            if entry.get("modifiers"):
                modifiers = entry.get("modifiers").split(",")
                modifiers = sorted(modifiers, key=lambda x: modifier_order.index(x) if x in modifier_order else len(modifier_order))
                for modifier in modifiers:
                    print_debug(f"modifier down {modifier}")
                    key_module.keyDown(modifier)
                    active_modifiers.insert(0, modifier)  # we build a reverse list, as we want to release in the opposite order
                    
            if entry.get("hold"):
                print_debug(f"press and hold{entry['hold']}: {modifier}")
                key_module.keyDown(key)
                time.sleep(entry["hold"])
                key_module.keyUp(key)
            elif entry.get("typewrite"):  # added very buggy, as it is keyboard-layout sensitive :(
                print_debug(f"typewrite: {entry.get('typewrite')}")
                key_module.typewrite(entry["typewrite"], interval=0.1)
            else:
                print_debug(f'press {key}')
                key_module.press(key, interval=0.005)

            if entry.get("modifier"):
                print_debug(f'down {entry.get("modifier")}')
                key_module.keyUp(entry.get("modifier"))

            if len(active_modifiers) > 0:
                for modifier in active_modifiers:
                    print_debug(f"modifier up {modifier}")
                    key_module.keyUp(modifier)

            if entry.get("wait"):
                print_debug(f"waiting {entry.get('wait')}")
                time.sleep(entry["wait"])

    def _context_switch_tool(self, current_context: AIContext = None) -> list[dict]:
        ai_context_values = [context.value for context in self.AIContext if context != current_context]

        tools = {
                "type": "function",
                "function": {
                    "name": "switch_context",
                    "description": "The context of the player request",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "context_name": {
                                "type": "string",
                                "description": "The available context the player conversion can be switched to",
                                "enum": ai_context_values
                            }
                        },
                        "required": ["context_name"]
                    }
                }
            }
        return tools
    
    def _get_keybinding_commands(self) -> list[dict]:
        # get defined commands in config
        commands = [
            command["name"]
            for command in self.config.get("commands", [])
            if not command.get("instant_activation")
        ]

        command_set = set(commands)
        sc_commands = set(self.sc_keybinding_service.get_bound_keybinding_names())

        command_set.update(sc_commands)  # manually defined commands overwrites sc_keybindings

        tools = {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Executes a player command. On successfull response, do follow the instructions in the response.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command_name": {
                                "type": "string",
                                "description": "The command to execute",
                                "enum": list(command_set)
                            }
                        },
                        "required": ["command_name"]
                    }
                }
            }
        return tools
    
    def _get_cora_tools(self) -> list[dict]:
        tools = []
        tools.append(self._get_keybinding_commands())
        tools.append(self._context_switch_tool(current_context=self.AIContext.CORA))
        return tools
    
    def _get_tdd_tools(self) -> list[dict]:

        tradeport_names = self.uex_service.get_category_names("tradeports")
        planet_names = self.uex_service.get_category_names("planets")
        satellite_names = self.uex_service.get_category_names("satellites")
        commodity_names = self.uex_service.get_category_names("commodities")
        cities_names = self.uex_service.get_category_names("cities")

        combined_locations_names = planet_names + satellite_names + cities_names + tradeport_names

        # commands = all defined keybinding label names
        tools = [
            {
                "type": "function",
                "function": 
                {
                    "name": "find_best_trade_route_from_location",
                    "description": "Identifies the best traderoute from a given tradeport, moon / satellite or planetary system.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location_name": {
                                "type": "string",
                                "description": "The location name from where to start the trade route. Can be the name of a planet, a moon / satellite or a specific tradeport",
                                "enum": combined_locations_names
                            },
                            "include_illegal_commodities": {
                                "type": "boolean",
                                "description": "Indicates if illegal or restricted commodities should be searched as well."
                            }
                        },
                        "required": ["location_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": 
                {
                    "name": "find_best_trade_route_between_locations",
                    "description": "Identifies the best traderoute between one location to another. The location is usually on planetary system scale, but can be more precise as well like moon / satellite or a specific tradeport",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location_name_from": {
                                "type": "string",
                                "description": "The location name from where to start the trade route. Can be the name of a planet, a moon / satellite or a specific tradeport.",
                                "enum": combined_locations_names
                            },
                            "location_name_to": {
                                "type": "string",
                                "description": "The location name where the player is heading to. Can be the name of a planet, a moon / satellite or a specific tradeport.",
                                "enum": combined_locations_names
                            },
                            "include_illegal_commodities": {
                                "type": "boolean",
                                "description": "Indicates if illegal or restricted commodities should be searched as well."
                            }
                        },
                        "required": ["location_name_from", "location_name_to"]
                    }
                }
            },
            {
                "type": "function",
                "function": 
                {
                    "name": "find_best_sell_price_for_commodity_at_location",
                    "description": "For a given commodity, identifies the best selling tradeport at a given location the player wants to go. The location is usually on planetary system scale, but can be more precise as well like moon / satellite or a specific tradeport",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location_name_to": {
                                "type": "string",
                                "description": "The location name specifies the area where to find a tradeport to sell the commodity.",
                                "enum": planet_names + satellite_names
                            },
                            "commodity_name": {
                                "type": "string",
                                "description": "The commodity that should be sold.",
                                "enum": commodity_names
                            },
                            "search_illegal": {
                                "type": "boolean",
                                "description": "Indicates if illegal or restricted commodities should be searched as well."
                            }
                        },
                        "required": ["location_name_to", "commodity_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": 
                {
                    "name": "find_best_trade_route_for_commodity",
                    "description": "For a given commodity, what the most profitable trade route is existing",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "commodity_name": {
                                "type": "string",
                                "description": "The commodity that should be sold.",
                                "enum": commodity_names
                            }
                        },
                        "required": ["commodity_code"]
                    }
                }
            },
            {
                "type": "function",
                "function": 
                {
                    "name": "find_best_selling_location_for_commodity",
                    "description": "For a given commodity, it finds the best location to sell it with the highest sell price.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "commodity_name": {
                                "type": "string",
                                "description": "The commodity that should be sold. ",
                                "enum": commodity_names
                            }
                        },
                        "required": ["commodity_code"]
                    }
                }
            },
            {
                "type": "function",
                "function": 
                {
                    "name": "switch_tdd_employee",
                    "description": "Whenever the player adresses a new Trading Devision Location, make this function call.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "employee_id": {
                                "type": "string",
                                "description": "The id of the employee that will respond to the player request",
                                "enum": self.config["openai"]["contexts"]["tdd_voices"].split(",")
                            }
                        },
                        "required": ["employee_id"]
                    }
                }
            }

        ]
        tools.append(self._context_switch_tool(current_context=self.AIContext.TDD))
        return tools

    # def __ask_gpt_for_locale(self, language: str) -> str:
