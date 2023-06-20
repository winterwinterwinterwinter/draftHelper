import datetime
import discord
import asyncio
from discord.ext import commands, tasks
from peewee import *
import pendulum
import os

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# Connect to the SQLite database
db = SqliteDatabase('draftbot.db')

# Define BaseModel first
class BaseModel(Model):
    class Meta:
        database = db

class Draft(BaseModel):
    draft_name = CharField()
    draft_datetime = DateTimeField()
    number_of_rounds = IntegerField()
    draft_budget = IntegerField()
    guild_id = IntegerField()
    channel_id = IntegerField()

    def time_until_draft_starts(self):
        now = pendulum.now()
        return (self.draft_datetime - now).total_seconds()

    async def run_draft(self, bot):
        try:
            await asyncio.sleep(self.time_until_draft_starts())  # Wait until the draft starts

            guild = bot.get_guild(self.guild_id)
            channel = guild.get_channel(self.channel_id)
            participants = [guild.get_member(participant.user_id) for participant in self.participants]

            private_thread = await channel.start_private_thread(name=f"{self.draft_name} Draft")
            for participant in participants:
                await private_thread.add_member(participant)

            for round_number in range(self.number_of_rounds):
                await private_thread.send(f"Round {round_number + 1} of {self.number_of_rounds}")

                for participant in participants:
                    remaining_budget = Participant.get(Participant.user_id == participant.id).budget
                    await private_thread.send(f"{participant.mention}, it's your turn to draft a Pokémon. You have {remaining_budget} budget left.")
                    await private_thread.send("Please enter the name of the Pokémon you want to draft:")
                    try:
                        def check(m):
                            return m.author == participant and m.channel == private_thread and m.content.lower() in [pokemon.name.lower() for pokemon in self.legal_pokemon]
                        message = await bot.wait_for('message', check=check, timeout=300)
                        chosen_pokemon = next((pokemon for pokemon in self.legal_pokemon if pokemon.name.lower() == message.content.lower()), None)
                        if chosen_pokemon and chosen_pokemon.value <= remaining_budget:
                            # Deduct from budget and continue
                            participant.budget -= chosen_pokemon.value
                            participant.save()
                        else:
                            await private_thread.send(f"{participant.mention}, you do not have enough budget for this Pokémon or the Pokémon is not legal. You have been removed from the draft.")
                            participants.remove(participant)
                    except asyncio.TimeoutError:
                        await private_thread.send(f"{participant.mention}, you took too long to respond. You have been removed from the draft.")
                        participants.remove(participant)

            await private_thread.send("The draft has ended. Thank you all for participating!")
        except Exception as e:
            print(f"An error occurred while starting the draft: {e}")

class Pokemon(BaseModel):
    name = CharField(unique=True)
    value = IntegerField()

class Participant(BaseModel):
    user_id = IntegerField()
    draft = ForeignKeyField(Draft, backref='participants')
    budget = IntegerField()  # Budget is set when creating a participant

class DraftPokemon(BaseModel):
    draft = ForeignKeyField(Draft, backref='drafts')
    pokemon = ForeignKeyField(Pokemon, backref='pokemons')

db.connect()
db.create_tables([Draft, Pokemon, Participant, DraftPokemon])

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    check_draft_start.start()

@tasks.loop(seconds=60)
async def check_draft_start():
    now = datetime.datetime.utcnow()
    upcoming_drafts = Draft.select().where(Draft.draft_datetime >= now)

    for draft in upcoming_drafts:
        if draft.time_until_draft_starts() == 0:
            await draft.run_draft(bot)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if 'hello' in message.content.lower():
        await message.channel.send('Hello!')
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send("Invalid command. Please check the command and try again.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("You're missing some required arguments.")
    else:
        await ctx.send("An error occurred while processing your command. Please try again later.")

@bot.command()
async def setDraft(ctx):
    try:
        # Initial reply to the user
        await ctx.send("Okay, let's setup a draft! What's the name?")
        draft_name = await bot.wait_for('message', check=lambda message: message.author == ctx.author, timeout=60.0)

        await ctx.send(f"Draft name is set as {draft_name.content}. When should this happen? (please format as YYYY-MM-DD HH:MM in 24-hour format)")
        datetime_str = await bot.wait_for('message', check=lambda message: message.author == ctx.author, timeout=60.0)

        draft_datetime = pendulum.parse(datetime_str.content)

        await ctx.send(f"Draft date and time is set as {draft_datetime}. Now, how many rounds should the draft have?")
        num_rounds_str = await bot.wait_for('message', check=lambda message: message.author == ctx.author, timeout=60.0)
        number_of_rounds = int(num_rounds_str.content)

        await ctx.send(f"The draft will have {number_of_rounds} rounds. What should be the budget for each participant?")
        draft_budget_str = await bot.wait_for('message', check=lambda message: message.author == ctx.author, timeout=60.0)
        draft_budget = int(draft_budget_str.content)

        await ctx.send(f"Each participant's budget will be {draft_budget}. Now, let's set the legal Pokémon for the draft. Please separate each name with a comma.")
        legal_pokemon_response = await bot.wait_for('message', check=lambda message: message.author == ctx.author, timeout=60.0)

        legal_pokemon_input = legal_pokemon_response.content.split(',')
        legal_pokemon = []
        for pokemon_value_str in legal_pokemon_input:
            pokemon_name, pokemon_value = pokemon_value_str.split('-')
            pokemon, created = Pokemon.get_or_create(name=pokemon_name, defaults={'value': int(pokemon_value)})
            if not created:
                pokemon.value = int(pokemon_value)
                pokemon.save()
            legal_pokemon.append(pokemon)

        await ctx.send(f"Legal Pokémon are set as {', '.join([pokemon.name for pokemon in legal_pokemon])}. Finally, who are the participants? Please @mention each participant, separated by a space.")
        participants_response = await bot.wait_for('message', check=lambda message: message.author == ctx.author, timeout=60.0)
        participants = [str(user.id) for user in participants_response.mentions]

        await ctx.send(f"Participants are set as {', '.join([f'<@{user}>' for user in participants])}.")

        try:
            with db.atomic():
                draft = Draft.create(
                    draft_name=draft_name.content,
                    draft_datetime=draft_datetime,
                    number_of_rounds=number_of_rounds,
                    draft_budget=draft_budget,
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id
                )

                for user_id in participants:
                    Participant.create(user_id=user_id, draft=draft, budget=draft_budget)
                
                for pokemon in legal_pokemon:
                    DraftPokemon.create(draft=draft, pokemon=pokemon)
        except Exception as e:
            await ctx.send(f"An error occurred while setting up the draft: {e}")
            return

        await ctx.send(f"Your draft has been set! Here are the details:\n- Name: {draft.draft_name}\n- Date and Time: {draft.draft_datetime}\n- Rounds: {number_of_rounds}\n- Participant Budget: {draft_budget}\n- Legal Pokémon: {', '.join([pokemon.name for pokemon in legal_pokemon])}\n- Participants: {', '.join([f'<@{user}>' for user in participants])}")
    except Exception as e:
        await ctx.send(f"An error occurred while setting up the draft: {e}")

@tasks.loop(seconds=60)
async def check_draft_start():
    now = datetime.datetime.utcnow()
    upcoming_drafts = Draft.select().where(Draft.draft_datetime >= now)

    for draft in upcoming_drafts:
        if draft.time_until_draft_starts() == 0:
            await draft.run_draft(bot)


@bot.command()
async def pokemonLeft(ctx):
    draft = Draft.get(Draft.guild_id == ctx.guild.id)
    drafted_pokemon = [draft_pokemon.pokemon for draft_pokemon in draft.drafts]
    remaining_pokemon = [pokemon for pokemon in draft.legal_pokemon if pokemon not in drafted_pokemon]

    if remaining_pokemon:
        pokemon_list = "\n".join([f"- {pokemon.name} ({pokemon.value})" for pokemon in remaining_pokemon])
        await ctx.send(f"Remaining Pokémon:\n{pokemon_list}")
    else:
        await ctx.send("No Pokémon remaining.")

@bot.command()
async def about(ctx, participant: discord.Member):
    draft = Draft.get(Draft.guild_id == ctx.guild.id)
    participant_entry = Participant.get((Participant.draft == draft) & (Participant.user_id == participant.id))
    selected_pokemon = [draft_pokemon.pokemon for draft_pokemon in participant_entry.participant.drafts]

    remaining_budget = participant_entry.budget
    selected_pokemon_list = "\n".join([f"- {pokemon.name}" for pokemon in selected_pokemon])

    await ctx.send(f"Participant: {participant.mention}\nRemaining Budget: {remaining_budget}\nSelected Pokémon:\n{selected_pokemon_list}")

@bot.command()
async def history(ctx):
    draft = Draft.get(Draft.guild_id == ctx.guild.id)
    draft_rounds = {}

    for draft_round in range(1, draft.number_of_rounds + 1):
        draft_rounds[draft_round] = []

    for draft_pokemon in draft.drafts:
        draft_rounds[draft_pokemon.round].append(f"- {draft_pokemon.pokemon.name} (Drafted by {draft_pokemon.participant.user_id})")

    history_list = []
    for draft_round, pokemon_list in draft_rounds.items():
        round_info = f"Round {draft_round}:\n" + "\n".join(pokemon_list)
        history_list.append(round_info)

    history_text = "\n\n".join(history_list)
    await ctx.send(f"Draft History:\n{history_text}")

@bot.event
async def on_disconnect():
    db.close()

bot.remove_command('help')

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="Draft Bot Help", description="List of available commands:")

    if ctx.channel.category_id == bot.get_cog("DraftCog").category_id:
        # Inside the Draft category
        embed.add_field(name="!setDraft", value="Starts the process of setting up a draft.", inline=False)
        embed.add_field(name="!pokemonLeft", value="Shows the remaining Pokémon in the draft.", inline=False)
        embed.add_field(name="!about [participant]", value="Shows the budget and selected Pokémon of the specified participant.", inline=False)
        embed.add_field(name="!history", value="Shows the draft history.", inline=False)
    else:
        # Outside the Draft category
        embed.add_field(name="!help", value="Displays this help message.", inline=False)
    
    await ctx.send(embed=embed)



bot.run(os.getenv('YOUR_BOT_TOKEN'))
