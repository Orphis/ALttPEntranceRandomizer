import aioconsole
import argparse
import asyncio
import functools
import json
import logging
import re
import urllib.request
import websockets

import Items
import Regions
from MultiClient import ReceivedItem

def get_room_info(ctx):
    return {
        'password': ctx.password is not None,
        'slots': ctx.players,
        'players': [(client.name, client.team, client.slot) for client in ctx.clients if client.auth is True]
    }

def same_name(lhs, rhs):
    return lhs.lower() == rhs.lower()

def same_team(lhs, rhs):
    return (type(lhs) is type(rhs)) and ((not lhs and not rhs) or (lhs.lower() == rhs.lower()))

async def send_msgs(websocket, msgs):
    if not websocket or not websocket.open or websocket.closed:
        return
    try:
        await websocket.send(json.dumps(msgs))
    except websockets.ConnectionClosed:
        pass

def broadcast_all(ctx, msgs):
    for client in ctx.clients:
        if client.auth:
            asyncio.create_task(send_msgs(client.socket, msgs))

def broadcast_team(ctx, team, msgs):
    for client in ctx.clients:
        if client.auth and same_team(client.team, team):
            asyncio.create_task(send_msgs(client.socket, msgs))

def notify_all(ctx, text):
    print("Notice (all): %s" % text)
    broadcast_all(ctx, [['Print', text]])

def notify_team(ctx, team, text):
    print("Team notice (%s): %s" % ("Default" if not team else team, text))
    broadcast_team(ctx, team, [['Print', text]])

def notify_client(client, text):
    if not client.auth:
        return
    print("Player notice (%s): %s" % (client.name, text))
    asyncio.create_task(send_msgs(client.socket,  [['Print', text]]))

async def server(websocket, path, ctx):
    client = Client(websocket)
    ctx.clients.append(client)

    try:
        await on_client_connected(ctx, client)
        async for data in websocket:
            for msg in json.loads(data):
                if len(msg) == 1:
                    cmd = msg
                    args = None
                else:
                    cmd = msg[0]
                    args = msg[1]
                await process_client_cmd(ctx, client, cmd, args)
    except Exception as e:
        if type(e) is not websockets.ConnectionClosed:
            logging.exception(e)
    finally:
        await on_client_disconnected(ctx, client)
        ctx.clients.remove(client)

async def on_client_connected(ctx, client):
    await send_msgs(client.socket, [['RoomInfo', get_room_info(ctx)]])

async def on_client_disconnected(ctx, client):
    if client.auth:
        await on_client_left(ctx, client)

async def on_client_joined(ctx, client):
    notify_all(ctx, "%s has joined the game as player %d for %s" % (client.name, client.slot, "the default team" if not client.team else "team %s" % client.team))

async def on_client_left(ctx, client):
    notify_all(ctx, "%s (Player %d, %s) has left the game" % (client.name, client.slot, "Default team" if not client.team else "Team %s" % client.team))

def get_player_name_in_team(ctx, team, slot):
    for client in ctx.clients:
        if client.auth and same_team(team, client.team) and client.slot == slot:
            return client.name
    return "Player %d" % slot

def get_client_from_name(ctx, name):
    for client in ctx.clients:
        if client.auth and same_name(name, client.name):
            return client
    return None

def get_received_items(ctx, team, player):
    for (c_team, c_id), items in ctx.received_items.items():
        if c_id == player and same_team(c_team, team):
            return items
    ctx.received_items[(team, player)] = []
    return ctx.received_items[(team, player)]

def tuplize_received_items(items):
    return [(item.name, item.location, item.player_id, item.player_name) for item in items]

def send_new_items(ctx):
    for client in ctx.clients:
        if not client.auth:
            continue
        items = get_received_items(ctx, client.team, client.slot)
        if len(items) > client.send_index:
            asyncio.create_task(send_msgs(client.socket, [['ReceivedItems', (client.send_index, tuplize_received_items(items)[client.send_index:])]]))
            client.send_index = len(items)

def forfeit_player(ctx, team, slot, name):
    all_locations = [locname for locname, values in Regions.location_table.items() if type(values[0]) is int]
    notify_all(ctx, "%s (Player %d) in team %s has forfeited" % (name, slot, team if team else 'default'))
    register_location_checks(ctx, name, team, slot, all_locations)

def register_location_checks(ctx, name, team, slot, locations):
    for location in locations:
        if (location, slot) in ctx.spoiler:
            target_item, target_player = ctx.spoiler[(location, slot)]
            if target_player != slot:
                found = False
                recvd_items = get_received_items(ctx, team, target_player)
                for recvd_item in recvd_items:
                    if recvd_item.location == location and recvd_item.player_id == slot:
                        found = True
                        break
                if not found:
                    new_item = ReceivedItem(target_item, location, slot, name)
                    recvd_items.append(new_item)
                    notify_team(ctx, team, '(Team) %s sent "%s" to %s (%s)' % (name, target_item, get_player_name_in_team(ctx, team, target_player), location))
    send_new_items(ctx)

async def process_client_cmd(ctx, client, cmd, args):
    if type(cmd) is not str:
        await send_msgs(client.socket, [['InvalidCmd']])
        return

    if cmd == 'Connect':
        if not args or type(args) is not dict or \
                'password' not in args or type(args['password']) not in [str, type(None)] or \
                'name' not in args or type(args['name']) is not str or \
                'team' not in args or type(args['team']) not in [str, type(None)] or \
                'slot' not in args or type(args['slot']) not in [int, type(None)]:
            await send_msgs(client.socket, [['InvalidArguments', 'Connect']])
            return

        errors = set()
        if ctx.password is not None and ('password' not in args or args['password'] != ctx.password):
            errors.add('InvalidPassword')

        if 'name' not in args or not args['name'] or not re.match(r'\w{1,10}', args['name']):
            errors.add('InvalidName')
        elif any([same_name(c.name, args['name']) for c in ctx.clients if c.auth]):
            errors.add('NameAlreadyTaken')
        else:
            client.name = args['name']

        if 'team' in args and args['team'] is not None and not re.match(r'\w{1,15}', args['team']):
            errors.add('InvalidTeam')
        else:
            client.team = args['team'] if 'team' in args else None

        if 'slot' in args and any([c.slot == args['slot'] for c in ctx.clients if c.auth and same_team(c.team, client.team)]):
            errors.add('SlotAlreadyTaken')
        elif 'slot' not in args or not args['slot']:
            for slot in range(1, ctx.players + 1):
                if slot not in [c.slot for c in ctx.clients if c.auth and same_team(c.team, client.team)]:
                    client.slot = slot
                    break
                elif slot == ctx.players:
                    errors.add('SlotAlreadyTaken')
        elif args['slot'] not in range(1, ctx.players + 1):
            errors.add('InvalidSlot')
        else:
            client.slot = args['slot']

        if errors:
            client.name = None
            client.team = None
            client.slot = None
            await send_msgs(client.socket, [['ConnectionRefused', list(errors)]])
        else:
            client.auth = True
            reply = [['Connected', ctx.rom_names[client.slot]]]
            items = get_received_items(ctx, client.team, client.slot)
            if items:
                reply.append(['ReceivedItems', (0, tuplize_received_items(items))])
                client.send_index = len(items)
            await send_msgs(client.socket, reply)
            await on_client_joined(ctx, client)

    if not client.auth:
        return

    if cmd == 'Sync':
        items = get_received_items(ctx, client.team, client.slot)
        if items:
            client.send_index = len(items)
            await send_msgs(client.socket, ['ReceivedItems', (0, tuplize_received_items(items))])

    if cmd == 'LocationChecks':
        if type(args) is not list:
            await send_msgs(client.socket, [['InvalidArguments', 'LocationChecks']])
            return
        register_location_checks(ctx, client.name, client.team, client.slot, args)

    if cmd == 'Say':
        if type(args) is not str or not args.isprintable():
            await send_msgs(client.socket, [['InvalidArguments', 'Say']])
            return

        notify_all(ctx, client.name + ': ' + args)

        if args[:8] == '!players':
            auth_clients = [c for c in ctx.clients if c.auth]
            auth_clients.sort(key=lambda c: ('' if not c.team else c.team.lower(), c.name))
            current_team = 0
            text = ''
            for c in auth_clients:
                if c.team != current_team:
                    text += '::' + ('default team' if not c.team else c.team) + ':: '
                    current_team = c.team
                text += '%d:%s ' % (c.slot, c.name)
            notify_all(ctx, 'Connected players: ' + text[:-1])
        if args[:8] == '!forfeit':
            forfeit_player(ctx, client.team, client.slot, client.name)

def set_password(ctx, password):
    ctx.password = password
    print('Password set to ' + password if password is not None else 'Password disabled')

async def console(ctx):
    while True:
        input = await aioconsole.ainput()

        command = input.split()
        if not command:
            continue

        if command[0] == '/password':
            set_password(ctx, command[1] if len(command) > 1 else None)
        if command[0] == '/kick' and len(command) > 1:
            client = get_client_from_name(ctx, command[1])
            if client and client.socket and not client.socket.closed:
                await client.socket.close()

        if command[0] == '/forfeitslot' and len(command) == 3 and command[2].isdigit():
            team = command[1] if command[1] != 'default' else None
            slot = int(command[2])
            name = get_player_name_in_team(ctx, team, slot)
            forfeit_player(ctx, team, slot, name)
        if command[0] == '/forfeitplayer' and len(command) > 1:
            client = get_client_from_name(ctx, command[1])
            if client:
                forfeit_player(ctx, client.team, client.slot, client.name)
        if command[0] == '/senditem' and len(command) > 2:
            [(player, item)] = re.findall(r'\S* (\S*) (.*)', input)
            if item in Items.item_table:
                client = get_client_from_name(ctx, player)
                if client:
                    new_item = ReceivedItem(item, "cheat console", 0, "server")
                    get_received_items(ctx, client.team, client.slot).append(new_item)
                    notify_all(ctx, 'Cheat console: sending "' + item + '" to ' + client.name)
                send_new_items(ctx)
            else:
                print("Unknown item: " + item)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', default=38281, type=int)
    parser.add_argument('--password', default=None)
    args = parser.parse_args()

    ctx = Context(args.host, args.port, args.password)
    ctx.server = websockets.serve(functools.partial(server,ctx=ctx), ctx.host, ctx.port)

    try:
        with open('multidata') as f:
            item_table = dict()
            for name, values in Items.item_table.items():
                if type(values[3]) is int:
                    assert(values[3] not in item_table)
                    item_table[values[3]] = name

            location_table = dict()
            for name, values in Regions.location_table.items():
                if type(values[0]) is int:
                    assert(values[0] not in location_table)
                    location_table[values[0]] = name
            assert len(location_table) is 216

            data = json.loads(f.read())
            ctx.players = data["players"]
            for player in range(1, ctx.players + 1):
                ctx.rom_names[player] = data["rom_names"][str(player)]
                for address, [item_code, item_player] in data[str(player)].items():
                    ctx.spoiler[(location_table[int(address)], player)] = (item_table[item_code], item_player)
    except Exception as e:
        print('Failed to read multiworld data (%s)' % e)
        raise e

    ip = urllib.request.urlopen('https://v4.ident.me').read().decode('utf8') if not ctx.host else ctx.host
    print('Hosting game of %d players (%s) at %s:%d' % (ctx.players, 'No password' if not ctx.password else 'Password: %s' % ctx.password, ip, ctx.port))

    await ctx.server
    await console(ctx)

class Client:
    def __init__(self, socket):
        self.socket = socket
        self.auth = False
        self.name = None
        self.team = None
        self.slot = None
        self.send_index = 0

class Context:
    def __init__(self, host, port, password):
        self.players = None
        self.rom_names = {}
        self.spoiler = {}
        self.host = host
        self.port = port
        self.password = password
        self.server = None
        self.clients = []
        self.received_items = {}

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    loop.close()