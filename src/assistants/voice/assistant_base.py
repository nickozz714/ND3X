import json
from typing import Any
class BaseAssistant:
    name='BaseAssistant'
    instructions='Je bent een feitelijke NL assistent.'
    def system(self): return [{'role':'system','content': self.instructions}]

    import json


    def extract_first_json_object(self, text: str) -> Any:
        """
        Zoekt in een willekeurige string naar het eerste geldige JSON-object of -array
        en parsed dat naar een Python object (dict / list).

        Werkt ook als er rommel vóór of ná het JSON staat, zoals:
        "Searching corpus... { ... echte json ... } bla bla"

        Raises:
            ValueError als er geen geldig JSON-object gevonden wordt.
        """
        decoder = json.JSONDecoder()
        idx = 0
        length = len(text)

        while idx < length:
            ch = text[idx]
            # JSON begint in de praktijk bijna altijd met { of [
            if ch not in "{[":
                idx += 1
                continue

            try:
                obj, end = decoder.raw_decode(text, idx)
                return obj
            except json.JSONDecodeError:
                # Dit was geen geldig JSON-begin → verder zoeken
                idx += 1

        raise ValueError("No valid JSON object or array found in model output.")
