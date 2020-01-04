import json

def pythonify(json_data):
    str_keys = []
    for key, value in json_data.items():
        if isinstance(value, list):
            value = [pythonify(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            value = pythonify(value)

        if isinstance(key, str):
            str_keys.append(key)

    for key in str_keys:
        try:
            newkey = int(key)
            json_data[newkey] = json_data[key]
            del json_data[key]
        except ValueError:
            pass

    return json_data

class MultiWorld:
    def __init__(self):
        self.players = None
        self.rom_names = {}
        self.locations = {}

    @classmethod
    def load(cls, name):
        with open(name, 'r') as f:
            json_data = pythonify(json.load(f))

            multiworld = MultiWorld()
            multiworld.players = json_data['players']
            multiworld.rom_names = json_data['rom_names']
            multiworld.locations = json_data['locations']

            return multiworld
    
    def write(self, name):
        with open(name, 'w') as f:
            json.dump(self.__dict__, f)
