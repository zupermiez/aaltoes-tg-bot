import asyncio
import logging
import secrets
import sys
import os
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any, Union
import pytz
from aiogram import Bot, Dispatcher, html, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters.command import Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from settings import phrases, info_message, help_message
from random import choice
import json
from upstash_redis import Redis
import json
from sgqlc.endpoint.http import HTTPEndpoint
from sgqlc.operation import Operation
from schema import Query
from settings import settings, linear_per_page
from db import db
from repository.user import UserRepository
from repository.events import EventsRepository
from repository.reminders import RemindersRepository
from repository.books import BooksRepository
from repository.points import PointsRepository
from repository.borrowings import BorrowingsRepository
from repository.requests import RequestsRepository
from repository.points_requests import PointsRequestsRepository
from keyboards import create_events_keyboard, create_event_details_keyboard, create_books_keyboard, create_book_details_keyboard, create_instance_selection_keyboard, create_pending_borrowings_keyboard,create_approve_borrowing_keyboard, create_return_keyboard, create_apply_keyboard, create_confirm_keyboard, create_requests_keyboard, create_approve_request_keyboard, create_users_keyboard, create_update_user_keyboard, create_actions_keyboard, create_task_keyboard, create_points_requests_keyboard, create_approve_points_request_keyboard
from upstash_redis import Redis


# Initialize repositories
redis = Redis(url=settings.UPSTASH_REDIS_URL, token=settings.UPSTASH_REDIS_TOKEN)
user_repo = UserRepository()
events_repo = EventsRepository(api_key=settings.LUMA_API_KEY)
reminders_repo = RemindersRepository()
books_repo = BooksRepository()

borrowings_repo = BorrowingsRepository()
requests_repo = RequestsRepository()
points_requests_repo = PointsRequestsRepository()
points_repo = PointsRepository()
# Initialize storage
storage = MemoryStorage()

# Initialize dispatcher
dp = Dispatcher(storage=storage)

current_events: Dict[str, Dict] = {}
EVENTS_CACHE_DURATION = timedelta(hours=0.5)
OVERDUE_CHECK_INTERVAL = timedelta(hours=1)
OVERDUE_NOTIFICATION_INTERVAL = timedelta(hours=12)
TIME_REMINDER_INTERVAL = timedelta(days=1)
pending_borrowings: List[Dict] = []
users_with_access: List[Dict] = []
current_books: List[Dict] = []

current_requests: List[Dict] = []
# Global variables to store the placeholder file_ids
placeholder_file_id = None  # For main books list
placeholder_file_id_2 = None  # For individual book details
document_file_id = None  # For Startup Sauna access document

# Use Pinata's image optimization with reduced size (300px width) and WebP format
placeholder_file_1_url = 'https://amaranth-defiant-snail-192.mypinata.cloud/ipfs/bafybeiawgmucvhow67n6xclzly2zxb6hgchwgacgljdh6iprzzwlebkwqe?img-width=500&img-quality=80&img-format=webp'
placeholder_file_2_url = 'https://amaranth-defiant-snail-192.mypinata.cloud/ipfs/bafkreiberkd4tzafmhmsepnwbtw32ois4b4enfvcl23hoo6bbkhrmz2moe?img-width=500&img-quality=80&img-format=webp'

# Dictionary to store reminder tasks
reminder_tasks: Dict[str, asyncio.Task] = {}

class BorrowState(StatesGroup):
    SELECT_INSTANCE = State()  # Only need this state for direct instance input
    RETURN_BOOK = State()

class ApplyState(StatesGroup):
    MOTIVATION = State()
    CONFIRMATION = State()

class ConfirmState(StatesGroup):
    CONFIRMATION = State()

class ChangeNameState(StatesGroup):
    NEW_NAME = State()

class ClaimTaskState(StatesGroup):
    MOTIVATION = State()

async def refresh_events() -> None:
    """Refresh events data from the API"""
    global current_events
    try:
        events = await events_repo.get_upcoming_events()
        current_events = {event['id']: event for event in events}
        logging.info(f"Events refreshed at {datetime.now()}")
    except Exception as e:
        logging.error(f"Error refreshing events: {e}")

async def get_books(force_refresh: bool = False) -> List[Dict]:
    """Get books from cache or refresh if needed"""
    global current_books

    if current_books is None or force_refresh:
        current_books = await books_repo.get_all_books(db)
    
    return current_books

async def get_requests(force_refresh: bool = False) -> List[Dict]:
    """Get requests from cache or refresh if needed"""
    global current_requests

    pending = 0
    applied = 0

    if current_requests is None or force_refresh:
        pending_requests = await requests_repo.get_pending_requests(db)
        applied_requests = await requests_repo.get_applied_requests(db)
        current_requests = pending_requests + applied_requests
        pending, applied = len(pending_requests), len(applied_requests)
    return current_requests, (pending, applied)

def get_event_timezone(event: Dict[str, Any]) -> pytz.timezone:
    """Get timezone for an event from event data"""
    try:
        if event.get('timezone'):
            return pytz.timezone(event['timezone'])
    except Exception as e:
        logging.error(f"Error getting timezone from event data: {e}")
    
    # Fallback to Helsinki timezone if timezone data is not available
    return pytz.timezone('Europe/Helsinki')

def fmt_telegram(input: str) -> str:
    """Escape special characters for Telegram Markdown formatting"""
    if not input:
        return input
    return input.replace("(", "\\(").replace(")", "\\)").replace("_", "\\_").replace(".", "\\.").replace("-", "\\-")

async def get_users_with_access(force_refresh: bool = False):
    global users_with_access
    if not users_with_access or force_refresh:
        users_with_access = await requests_repo.get_users_with_access(db)
    return users_with_access

def format_event_time(event: Dict[str, Any]) -> str:
    """Format event time with timezone information"""
    try:
        # Get the event's timezone
        event_tz = get_event_timezone(event)
        
        # Ensure the input time is timezone-aware (UTC)
        if event['start_time'].tzinfo is None:
            utc_time = pytz.UTC.localize(event['start_time'])
        else:
            utc_time = event['start_time']
        
        # Convert to event's local time
        local_time = utc_time.astimezone(event_tz)
        
        return f"{local_time.strftime('%d %b %Y, %H:%M')}"
    except Exception as e:
        logging.error(f"Error formatting event time: {e}")
        # Fallback to simple formatting if there's an error
        return event['start_time'].strftime('%d %b %Y, %H:%M')

def format_datetime(dt: datetime) -> str:
    """Format datetime in a consistent, readable way"""
    if not dt:
        return "N/A"
    # Convert to Helsinki timezone if not already in it
    helsinki_tz = pytz.timezone('Europe/Helsinki')
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    local_dt = dt.astimezone(helsinki_tz)
    return local_dt.strftime('%d %b %Y, %H:%M')

def get_optimized_image_url(ipfs_url: str) -> str:
    """Get optimized image URL from Pinata IPFS URL"""
    if not ipfs_url or not ipfs_url.startswith('https://'):
        return ipfs_url
    # Add optimization parameters if not already present
    if '?img-width=' not in ipfs_url:
        return f"{ipfs_url}?img-width=500&img-quality=80&img-format=webp"
    return ipfs_url

async def get_linear_token():
    """Get Linear access token from Redis"""
    try:
        token_data = redis.get("cred:token:linear")
        if not token_data:
            logging.error("No Linear token found in Redis")
            return None
            
        try:
            parsed_data = json.loads(token_data)
            if not isinstance(parsed_data, dict) or "json" not in parsed_data or "accessToken" not in parsed_data["json"]:
                logging.error("Invalid token data format in Redis")
                return None
            return parsed_data["json"]["accessToken"]
        except json.JSONDecodeError as e:
            logging.error(f"Error parsing Linear token JSON: {e}")
            return None
            
    except Exception as e:
        logging.error(f"Unexpected error getting Linear token: {e}")
        return None

def format_priority(priority: int) -> str:
    """Format priority with emoji and text"""
    priority_map = {
        0: "🔵 No Priority",
        1: "🔴 Urgent",
        2: "🟠 High",
        3: "🟡 Medium",
        4: "🟢 Low"
    }
    return priority_map.get(priority, "⚪ Unknown")

async def get_issue_by_id(team: str, number: str):
    """
    Load Linear issues with pagination
    Args:
        team: Team name of the issue
        number: Issue number (will be converted to integer)
    """
    token_data = await get_linear_token()
    if not token_data:
        logging.error("Failed to get Linear token")
        return None

    try:
        # Convert number to integer
        try:
            issue_number = int(number)
        except ValueError:
            logging.error(f"Invalid issue number format: {number}")
            return None

        endpoint = HTTPEndpoint("https://api.linear.app/graphql", {"Authorization": token_data})
        op = Operation(Query)

        
        issue = op.issues(
            filter={"assignee": {"isMe": {"eq": True}}, "state": {"type": {"in": ["unstarted", "started"]}}, "number": {"eq": issue_number}, "team": {"key": {"eq": team}}}
        )
        
        issue.nodes.id()
        issue.nodes.number()
        issue.nodes.team.key()
        issue.nodes.title()
        issue.nodes.description()
        issue.nodes.estimate()
        issue.nodes.labels.nodes.name()
        issue.nodes.due_date()
        issue.nodes.priority()
        issue.nodes.state.name()
        issue.nodes.project.name()
        issue.nodes.created_at()
        result = endpoint(op)['data']
        nodes = result.get("issues", {}).get("nodes", [])
        if not nodes:
            logging.error(f"No issue found for team {team} and number {issue_number}")
            return None
            
        node = nodes[0]
        
        # Parse due date if it exists
        due_date = None
        if node.get("dueDate"):
            try:
                due_date = datetime.fromisoformat(node["dueDate"].replace('Z', '+00:00'))
            except Exception as e:
                logging.error(f"Error parsing due date: {e}")
        try:
            project = node.get("project", {}).get("name")
        except Exception as e:
            project = "No Project"
        
        return {
            "id": node.get("id"),
            "number": node.get("number"),
            "title": node.get("title") or "No Title",
            "description": node.get("description") or "No Description",
            "points": node.get("estimate") or 1,
            "team": node.get("team", {}).get("key") or "No Team",
            "labels": [label.get("name") for label in node.get("labels", {}).get("nodes", [])] if node.get("labels", {}).get("nodes") else [],
            "due_date": due_date,
            "created_at": node.get("createdAt") or "No Created At",
            "priority": node.get("priority") or 0,  # Keep raw priority value
            "project": project,
            "state": node.get("state", {}).get("name") or "No State"
        }
    except Exception as e:
        logging.error(f"Error fetching issue from Linear: {e}")
        return None

@dp.message(Command("start"))
async def command_start_handler(message: Message) -> None:
    """
    This handler receives messages with `/start` command
    """
    # Save user to database
    await user_repo.save_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )

    if message.from_user.id in settings.ADMINS and message.from_user.id not in user_repo.get_admins():
        user_repo.set_admin(message.from_user.id)

    welcome_message = (
        f"👋 Hello, {html.bold(message.from_user.full_name)}!\n\n"
        f"Welcome to the Aaltoes Community Bot! \n\n"
        f"Type /help to see all available commands!"
    )

    await message.answer(welcome_message)

async def handle_help(message: Union[Message, CallbackQuery]) -> None:
    """Shared handler for help command and callback"""

    if isinstance(message, Message):
        await message.answer(help_message, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await message.message.answer(help_message, parse_mode=ParseMode.MARKDOWN_V2)

@dp.callback_query(F.data.startswith("action_help"))
async def help_callback_handler(callback: CallbackQuery) -> None:
    """Handle help button click"""
    await handle_help(callback)

@dp.message(Command("help"))
async def command_help_handler(message: Message) -> None:
    """Handle /help command"""
    await handle_help(message)

@dp.message(Command("info"))
async def command_info_handler(message: Message) -> None:
    """
    This handler receives messages with `/info` command
    """
    await message.answer(info_message, parse_mode=ParseMode.MARKDOWN)
    await message.answer_location(60.18785632704554, 24.823863548762827)


async def handle_confirm(message: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Shared handler for confirm command and callback"""
    user = await user_repo.get_user(message.from_user.id)
    if not user:
        user = await user_repo.save_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )
    if user['confirm'] == False:
        text = "Please send your full name to confirm your account. Example: John Doe\n\nYou can also send /cancel to cancel the confirmation."
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)
        await state.set_state(ConfirmState.CONFIRMATION)
    else:
        text = "You are already confirmed."
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)

@dp.callback_query(F.data.startswith("action_confirm"))
async def confirm_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle confirm button click"""
    await handle_confirm(callback, state)

@dp.message(Command("confirm"))
async def command_confirm_handler(message: Message, state: FSMContext) -> None:
    """Handle /confirm command"""
    await handle_confirm(message, state)

@dp.message(ConfirmState.CONFIRMATION)
async def process_confirmation(message: Message, state: FSMContext) -> None:
    """Process the confirmation state"""
    if message.text == "/cancel":
        await message.answer("Confirmation cancelled.")
        await state.clear()
    else:
        name = message.text
        await message.answer(f"Your full name is set to {name}. You can change it later in /menu")
        await user_repo.update_user(message.from_user.id, confirm=True, name=name)
        await state.clear()

async def handle_events(message: Union[Message, CallbackQuery]) -> None:
    """Shared handler for events command and callback"""
    if not current_events:
        await refresh_events()
        if not current_events:
            if isinstance(message, Message):
                await message.answer("No upcoming events found.")
            else:
                await message.message.answer("No upcoming events found.")
            return
    
    reply_markup = create_events_keyboard(current_events)
    message_text = f"📅 Upcoming Events"

    if isinstance(message, Message):
        await message.answer(message_text, reply_markup=reply_markup)
    else:
        await message.message.answer(message_text, reply_markup=reply_markup)

@dp.callback_query(F.data.startswith("action_events"))
async def events_callback_handler(callback: CallbackQuery) -> None:
    """Handle events button click"""
    await handle_events(callback)

@dp.message(Command("events"))
async def command_events_handler(message: Message) -> None:
    """Handle /events command"""
    await handle_events(message)

@dp.callback_query(F.data.startswith("event_"))
async def event_callback_handler(callback: CallbackQuery) -> None:
    """Handle event button clicks"""
    event_id = callback.data.split("_")[1]
    user_id = callback.from_user.id
    event = current_events.get(event_id)
    
    if not event:
        await callback.answer("❌ This event is no longer exists", show_alert=True)
    
    user_reminders = await reminders_repo.get_user_reminders(db, user_id)
    has_reminder = any(r['event_id'] == event_id for r in user_reminders)
    
    message_text = (
        f"*{fmt_telegram(event['title'])}*\n\n"
        f"📅 {format_event_time(event)}\n"
        f"📍 {event['location']}\n\n"
    )
    
    if event.get('url'):
        message_text += f"[Event Link]({event['url']})"
    
    
    await callback.message.edit_text(
        message_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=create_event_details_keyboard(event_id, has_reminder)
    )

@dp.callback_query(F.data == "back_to_events")
async def back_to_events_handler(callback: CallbackQuery) -> None:
    """Handle back button clicks"""
    
    if not current_events:
        await callback.message.edit_text("No events available.")
        return
    
    await callback.message.edit_text(
        f"📅 Upcoming Events",
        reply_markup=create_events_keyboard(current_events)
    )

@dp.message(Command("refresh"))
async def command_refresh_handler(message: Message, bot: Bot) -> None:
    """
    This handler receives messages with `/refresh_events` command
    Only admins can use this command
    """

    user = await user_repo.get_user(message.from_user.id)
    if user['role'] != 'admin':
        await message.answer("You are not authorized to use this command.")
    else:
        await refresh_events()
        # Get all admin users from database
        admins = await user_repo.get_admins()
        for admin in admins:
            try:
                await bot.send_message(admin['id'], f"Events refreshed at {datetime.now()}")
            except Exception as e:
                logging.error(f"Error sending message to admin {admin['id']}: {e}")
        global current_events
    
        if not current_events:
            await message.answer("❌ No upcoming events found after refresh.")
            return
        else:
            await message.answer("🔄 Events have been refreshed! Use /events to see the updated list.")
    

async def handle_reminders(message: Union[Message, CallbackQuery]) -> None:
    """Shared handler for reminders command and callback"""
    user_id = message.from_user.id
    user_reminders = await reminders_repo.get_user_reminders(db, user_id)
    
    if not user_reminders:
        text = "You don't have any reminders set."
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)
        return
    
    message_text = "🔔 Your reminders\n\n"
    for reminder in user_reminders:
        logging.info(f"Reminder: {reminder_tasks['reminder_' + str(reminder['event_id']) + '_' + str(user_id)]}")
        event = current_events.get(reminder['event_id'])
        if event:
            reminder_time = reminder['reminder_time'].astimezone(pytz.timezone('Europe/Helsinki'))
            message_text += (
                "-----------------------------------\n"
                f"📅 {event['title']}\n"
                f"⏰ Reminder at: {reminder_time.strftime('%d %b %Y, %H:%M')}\n"
                f"📍 {event['location']}\n\n"
            )
    
    if isinstance(message, Message):
        await message.answer(message_text)
    else:
        await message.message.answer(message_text)

@dp.callback_query(F.data.startswith("action_reminders"))
async def reminders_callback_handler(callback: CallbackQuery) -> None:
    """Handle reminders button click"""
    await handle_reminders(callback)

@dp.message(Command("reminders"))
async def command_reminders_handler(message: Message) -> None:
    """Handle /reminders command"""
    await handle_reminders(message)

@dp.callback_query(F.data.startswith("reminder_"))
async def reminder_callback_handler(callback: CallbackQuery) -> None:
    """Handle reminder button clicks"""
    event_id = callback.data.split("_")[1]
    user_id = callback.from_user.id
    event = current_events.get(event_id)
    
    if not event:
        await callback.answer("Event not found.")
        return
    
    # Check if reminder is already set
    user_reminders = await reminders_repo.get_user_reminders(db, user_id)
    if any(r['event_id'] == event_id for r in user_reminders):
        await callback.answer("You already have a reminder set for this event!")
        return
    
    # Schedule the reminder and store the task
    task = asyncio.create_task(schedule_reminder(callback.bot, user_id, event))
    task_key = f"reminder_{event_id}_{user_id}"
    reminder_tasks[task_key] = task

    message_text = (
        f"*{fmt_telegram(event['title'])}*\n\n"
        f"📅 {format_event_time(event)}\n"
        f"📍 {event['location']}\n\n"
    )
    
    if event.get('url'):
        message_text += f"[Event Link]({event['url']})"
    
    await callback.message.edit_text(
        message_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=create_event_details_keyboard(event_id, True)
    )

    await callback.answer("Reminder set! You'll receive a notification 1 hour before the event.")

@dp.callback_query(F.data.startswith("remove_reminder_"))
async def remove_reminder_handler(callback: CallbackQuery) -> None:
    """Handle remove reminder button clicks"""
    event_id = callback.data.split("_")[2]
    user_id = callback.from_user.id
    
    try:
        # Cancel the reminder task if it exists
        task_key = f"reminder_{event_id}_{user_id}"
        if task_key in reminder_tasks:
            task = reminder_tasks[task_key]
            task.cancel()
            del reminder_tasks[task_key]
        
        await reminders_repo.delete_reminder(db, user_id, event_id)

        event = current_events.get(event_id)
        if event:
            message_text = (
                f"*{fmt_telegram(event['title'])}*\n\n"
                f"📅 {format_event_time(event)}\n"
                f"📍 {event['location']}\n\n"
            )
        
            url = event.get('url')
            if url:
                message_text += f"[Event Link]({url})"
        # Update the message with the same text but new keyboard (without reminder)
        await callback.message.edit_text(
    
            message_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=create_event_details_keyboard(event_id, False)
        )
        await callback.answer("Reminder removed!")
    except Exception as e:
        logging.error(f"Error removing reminder: {e}")
        await callback.answer("❌ An error occurred while removing the reminder. Please try again.")

async def periodic_events_refresh() -> None:
    """Periodically refresh events data"""
    while True:  
        try:
            logging.info("Refreshing events data")
            await refresh_events()
            await asyncio.sleep(EVENTS_CACHE_DURATION.total_seconds())
        except Exception as e:
            logging.error(f"Error refreshing events data: {e}")
        
        await asyncio.sleep(EVENTS_CACHE_DURATION.total_seconds())

async def send_reminder(bot: Bot, user_id: int, event: Dict[str, Any]) -> None:
    """Send a reminder message to the user"""
    try:
        message = (
            f"🔔 Reminder: {event['title']} starts in 1 day!\n\n"
            f"📅 {format_event_time(event)}\n"
            f"📍 {event['location']}"
        )
        logging.info(f"Sending reminder to user {user_id} for event {event['title']}")
        await bot.send_message(user_id, message)

        try:    
            await reminders_repo.delete_reminder(db, user_id, event['id'])
        except Exception as e:
            logging.error(f"Reminder doesn't exist in database")
                
    except Exception as e:
        logging.error(f"Error sending reminder: {e}")

async def schedule_reminder(bot: Bot, user_id: int, event: Dict[str, Any]) -> None:
    """Schedule a reminder for the event"""
    try:
        # Calculate time until reminder (1 hour before event)
        now = datetime.now(pytz.UTC)
        reminder_time = event['start_time'] - TIME_REMINDER_INTERVAL
        
        if reminder_time <= now:
            # If event is less than 1 hour away, send reminder immediately
            await send_reminder(bot, user_id, event)
            return
        
        # Save reminder to database
        await reminders_repo.save_reminder(db, user_id, event['id'], reminder_time)
        
        # Calculate delay in seconds
        delay = (reminder_time - now).total_seconds()
        
        # Wait for the delay and send reminder
        await asyncio.sleep(delay)
        
        # check if reminder still exists
        if await reminders_repo.get_reminder(db, user_id, event['id']):
            await send_reminder(bot, user_id, event)
                
    except Exception as e:
        logging.error(f"Error scheduling reminder: {e}")


async def show_books_list(message: Union[Message, CallbackQuery], page: int = 0) -> None:
    """Show list of books with pagination"""
    global placeholder_file_id  # Access the global variable
    
    try:
        # Get books from cache
        books = await get_books()
        
        if not books:
            if isinstance(message, Message):
                await message.answer("No books available.")
            else:
                await message.message.edit_text("No books available.")
            return
        
        # Create keyboard with pagination using the provided page parameter
        reply_markup = create_books_keyboard(books, page)

        if isinstance(message, CallbackQuery):
            # Get the current photo file_id
            if placeholder_file_id:
                # Edit the existing photo message
                await message.message.edit_media(
                    media=InputMediaPhoto(
                        media=placeholder_file_id,
                        caption=f"Welcome to Aaltoes Library!"
                    ),
                    reply_markup=reply_markup
                )
            else:
                # First time sending the image - use optimized URL
                sent_message = await message.message.edit_media(
                    media=InputMediaPhoto(
                        media=get_optimized_image_url(placeholder_file_1_url),
                        caption=f"Welcome to Aaltoes Library!"
                    ),
                    reply_markup=reply_markup
                )
                # Store the file_id for future use
                placeholder_file_id = sent_message.photo[-1].file_id
                
        else:
            # If it's a new message, send a photo message
            if placeholder_file_id:
                # Use cached file_id
                await message.answer_photo(
                    photo=placeholder_file_id,
                    caption=f"Welcome to Aaltoes Library!",
                    reply_markup=reply_markup
                )
            else:
                # First time sending the image - use optimized URL
                sent_message = await message.answer_photo(
                    photo=get_optimized_image_url(placeholder_file_1_url),
                    caption=f"Welcome to Aaltoes Library!",
                    reply_markup=reply_markup
                )
                # Store the file_id for future use
                placeholder_file_id = sent_message.photo[-1].file_id
                
            
    except Exception as e:
        logging.error(f"Error in show_books_list: {e}")
        if isinstance(message, Message):
            await message.answer("❌ An error occurred while loading the books list. Please try again.")
        else:
            await message.message.answer("❌ An error occurred while loading the books list. Please try again.")

async def update_overdue_borrowings(bot: Bot) -> None:
    """Check for borrowings that need to be returned"""
    while True:
        logging.info("Checking for overdue borrowings")
        await borrowings_repo.update_overdue_borrowings(db)
        await asyncio.sleep(OVERDUE_CHECK_INTERVAL.total_seconds())

async def send_overdue_notification(bot: Bot) -> None:
    """Send a notification to the user that their borrowing is overdue"""
    while True:
        try:
            logging.info("Sending overdue notifications")
            overdue_borrowings = await borrowings_repo.get_overdue_borrowings(db)
            for borrowing in overdue_borrowings:
                await bot.send_message(borrowing['user_id'], f"Copy #{borrowing['instance_id']} is overdue. Please return it to the library.")
        except Exception as e:
            logging.error(f"Error sending overdue notifications: {e}")
        await asyncio.sleep(OVERDUE_NOTIFICATION_INTERVAL.total_seconds()) 

@dp.callback_query(F.data.startswith("instance_select_"))
async def book_instance_select_handler(callback: CallbackQuery) -> None:
    """Handle book instance selection"""
    book_id = int(callback.data.split("_")[2])
        
    books = await get_books(force_refresh=True) 

    book = next((b for b in books if b['book_id'] == book_id), None)
    if not book:
        await callback.answer("Book not found", show_alert=True)
        return
        
    instances = await books_repo.get_book_instances(db, book_id)

    if not instances:
        await callback.answer("No instances found", show_alert=True)
        return
        
    # Filter available instances
    available_instances = [i for i in instances if i['available']]

    if not available_instances:
        await callback.answer("No available copies", show_alert=True)
        return
    
    current_page = callback.data.split("_")[-1]
    keyboard = create_instance_selection_keyboard(available_instances, book_id, current_page)

    await callback.message.edit_caption(
        caption="Select a copy to borrow:",
        reply_markup=keyboard
    )
    

@dp.callback_query(F.data.startswith("instance_"))
async def book_instance_handler(callback: CallbackQuery) -> None:
    """Handle book instance selection"""
    user = await user_repo.get_user(callback.from_user.id)
    if user['confirm'] == False:
        await callback.answer("Please confirm your account first. Use /confirm command.", show_alert=True)
        return
    try:
        # Get instance_id and book_id from callback data
        # Format: book_instance_{instance_id}_{book_id}
        parts = callback.data.split("_")
        instance_id = int(parts[1])
        book_id = int(parts[2])
        
        # Get current instance status
        instances = await books_repo.get_book_instances(db, book_id)
        instance = next((i for i in instances if i['instance_id'] == instance_id), None)
        
        if not instance:
            await callback.answer("❌ Instance not found", show_alert=True)
            return
            
        if not instance['available']:
            await callback.answer("❌ This copy is no longer available", show_alert=True)
            return
        
        # Check if user has already borrowed this book
        borrowings = await borrowings_repo.get_user_borrowings(db, callback.from_user.id)

        if len(borrowings) >= 3:
            await callback.answer("❌ You have reached the maximum number of borrowings (3)", show_alert=True)
            return
        
        # Create borrowing record
        borrow_id = await borrowings_repo.create_borrowing(db, callback.from_user.id, instance_id)
        
        # Update instance availability
        await books_repo.update_book_availability(db, instance_id, False)
        
        # Get book details for success message
        books = await get_books(force_refresh=True)  # Refresh to get updated availability
        book = next((b for b in books if b['book_id'] == book_id), None)
        
        # Get borrowing details
        borrowing = await borrowings_repo.get_borrowing_by_instance(db, instance_id)
        return_time = borrowing['borrow_return_time'].strftime('%d %b %Y')
        
        success_message = (
            f"✅ Successfully borrowed\n\n"
            f"📚 {book['title']}\n"
            f"👤 Author: {book.get('author', 'Unknown')}\n"
            f"📖 Copy #{instance_id}\n"
            f"📅 Return by: {return_time}\n\n"
            f"Back to library /books"
        )
    
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=callback.message.photo[-1].file_id,
                caption=success_message
            )
        )
 
    except Exception as e:
        logging.error(f"Error in book_instance_handler: {e}")
        logging.error(f"Callback data: {callback.data}")
        await callback.answer("❌ An error occurred while booking the copy. Please try again.")

@dp.callback_query(F.data.startswith("books_page_"))
async def books_page_handler(callback: CallbackQuery) -> None:
    """Handle books pagination"""
    try:
        # Get page number from callback data
        page = int(callback.data.split("_")[2])
        logging.info(f"Handling page navigation to page {page}")
        
        # Get books from cache without forcing refresh
        books = await get_books(force_refresh=False)

        if not books:
            await callback.answer("No books available", show_alert=True)
            return
            
        reply_markup = create_books_keyboard(books, page)
        
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=placeholder_file_id,
                caption=f"Welcome to Aaltoes Library! "
            ),
            reply_markup=reply_markup
        )
            
    except Exception as e:
        logging.error(f"Error in books_page_handler: {str(e)}")
        logging.error(f"Callback data: {callback.data}")
        await callback.answer("An error occurred while changing pages", show_alert=True)

@dp.callback_query(F.data.startswith("book_"))
async def book_callback_handler(callback: CallbackQuery) -> None:
    """Handle book button clicks"""
    global placeholder_file_id_2  # Use the second placeholder for book details

    parts = callback.data.split("_")
    book_id = int(parts[1])
    current_page = int(parts[2]) if len(parts) > 2 else 0
    
    # Get books from cache without forcing refresh
    books = await get_books(force_refresh=False)
    book = next((b for b in books if b['book_id'] == book_id), None)
    
    if not book:
        await callback.answer("❌ Book not found", show_alert=True)
        return
    
    # Get book instances
    instances = await books_repo.get_book_instances(db, book_id)
    if not instances:
        await callback.answer("❌ No instances found for this book", show_alert=True)
        return
        
    instance_id = instances[0]['instance_id']
    
    # Build message text with optional fields
    message_text = f"📚 *{fmt_telegram(book.get('title', 'No title available'))}*\n\n"
    
    if book.get('author'):
        message_text += f"👤 Author: {book['author']}\n"
    
    if book.get('year'):
        message_text += f"📅 Year: {book['year']}\n"
    
    # Add availability info from instances
    total_instances = len(instances)
    available_instances = sum(1 for i in instances if i['available'])
    message_text += f"📖 Copies: {available_instances}/{total_instances} available\n\n"
    
    if book.get('description'):
        message_text += f"{fmt_telegram(book['description'])}\n\n"

    try:
        file_id = await books_repo.get_file_id(db, instance_id)
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=file_id,
                caption=message_text,
                parse_mode=ParseMode.MARKDOWN
            ),
            reply_markup=create_book_details_keyboard(book_id, current_page, available_instances)
        )
    except Exception as e:
        try:    
            image_path = instances[0]['image']
            sent_message = await callback.message.edit_media(
                media=InputMediaPhoto(
                media=get_optimized_image_url(image_path),
                caption=message_text,
                parse_mode=ParseMode.MARKDOWN
            ),
            reply_markup=create_book_details_keyboard(book_id, current_page, available_instances)
        )
            # Store the file_id in both memory and database
            file_id = sent_message.photo[-1].file_id
            await books_repo.save_file_id(db, instance_id, file_id)
        except Exception as e:
            if placeholder_file_id_2:
                # Use cached placeholder
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=placeholder_file_id_2,
                        caption=message_text,
                        parse_mode=ParseMode.MARKDOWN
                    ),
                    reply_markup=create_book_details_keyboard(book_id, current_page, available_instances)
                )
            else:
                # First time sending placeholder - use optimized URL
                sent_message = await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=get_optimized_image_url(placeholder_file_2_url),
                        caption=message_text,
                        parse_mode=ParseMode.MARKDOWN
                    ),
                    reply_markup=create_book_details_keyboard(book_id, current_page, available_instances)
                )
                placeholder_file_id_2 = sent_message.photo[-1].file_id

@dp.message(Command("books"))
async def command_books_handler(message: Message) -> None:
    """Handle /books command"""
    try:
        # Force refresh books if cache is stale
        books = await get_books(force_refresh=True)
        if not books:
            await message.answer("❌ No books available at the moment. Please try again later.")
            return
            
        await show_books_list(message)

    except Exception as e:
        logging.error(f"Error in /books command: {e}")
        await message.answer("❌ An error occurred while loading the books. Please try again.")

async def handle_borrowings(message: Union[Message, CallbackQuery]) -> None:
    """Shared handler for borrowings command and callback"""
    user = await user_repo.get_user(message.from_user.id)
    if user['confirm'] == False:
        await message.answer("Please confirm your account first. Use /confirm command.")
        return
    borrowings = await borrowings_repo.get_user_borrowings(db, message.from_user.id)
    
    text = f"You have {len(borrowings)} active borrowings. We check for overdue borrowings every hour. And notify you every 12 hours about overdue borrowings.\n"

    if len(borrowings) > 0:    
        text += "Press /return to return the book\n------------------------------\n"

    for borrowing in borrowings:
        logging.info(f"Processing borrowing: {borrowing}")
        text += f"📚 {borrowing['title']}\n"
        text += f"👤 Author: {borrowing['author']}\n"
        text += f"🖼️ Copy #{borrowing['instance_id']}\n"
        text += f"📅 Return by: {format_datetime(borrowing['borrow_return_time'])}\n"
        text += f"State: "
        if borrowing['state'] == 'overdue':
            text += "❌ Overdue\n"
        elif borrowing['state'] == 'pending':
            text += "⏳ Pending\n"
        elif borrowing['state'] == 'borrowed':
            text += "🤝 Borrowed\n"
        elif borrowing['state'] == 'returned':
            text += "✅ Returned\n"
        text +="------------------------------\n"

    pending_borrowings = await borrowings_repo.get_user_pending_borrowings(db, message.from_user.id)
    if pending_borrowings:
        m = len(pending_borrowings)

        for i, book in enumerate(pending_borrowings):
            text += f"Book #{book['instance_id']}"
            if i < m-1:
                text += ","
        if m > 1:
            text += " are still pending"
        else:
            text += " is still pending"
    
    if isinstance(message, Message):
        await message.answer(text)
    else:
        await message.message.answer(text)

@dp.callback_query(F.data.startswith("action_borrowings"))
async def borrowings_callback_handler(callback: CallbackQuery) -> None:
    """Handle borrowings button click"""
    await handle_borrowings(callback)

@dp.message(Command("borrowings"))
async def command_borrowings_handler(message: Message) -> None:
    """Handle /borrowings command"""
    await handle_borrowings(message)

@dp.message(Command("admin"))
async def command_admin_handler(message: Message) -> None:
    """Handle /admin command"""
    user = await user_repo.get_user(message.from_user.id)
    if user['role'] != 'admin':
        await message.answer("You are not authorized to use this command.")
    else:
        admin_commands = (
            "Admin Commands:\n\n"
            "/points - Show points requests\n"
            "/refresh - Refresh events list\n"
            "/refresh_books - Refresh books list\n"
            "/check - Check pending borrowings\n"
            "/admin - Show this help message\n\n"
            "/access - List users with Startup Sauna access and suspend access if needed\n"
            "/approve - Approve a requests for Startup Sauna access\n"
        )
        await message.answer(admin_commands)
   
@dp.message(Command("history"))
async def command_history_handler(message: Message) -> None:
    """Handle /history command"""
    borrowings = await borrowings_repo.get_user_borrowings_history(db, message.from_user.id)
    if not borrowings:
        await message.answer("You have no borrowings.")
        return
    text = f"Your borrowing history:\n"
    for i, borrowing in enumerate(borrowings):
        text+=f"{i+1}. Time: {format_datetime(borrowing['borrow_time'])}, "
        text+=f"Copy #{borrowing['instance_id']}\n"
    await message.answer(text)
    
@dp.message(Command("return"))
async def command_return_handler(message: Message, state: FSMContext) -> None:
    """Handle /return command"""
    borrowings = await borrowings_repo.get_user_borrowings(db, message.from_user.id)
    if not borrowings:
        await message.answer("You have no borrowings.")
        return
    
    await message.answer(
        "📚 Please select the book you want to return:",
        reply_markup=create_return_keyboard(borrowings)
    )
    await state.set_state(BorrowState.RETURN_BOOK)

@dp.message(BorrowState.RETURN_BOOK)
async def process_return_book(message: Message, state: FSMContext, bot: Bot) -> None:
    """Process book return by instance ID"""
    try:
        # Extract instance ID from "Copy #id" format
        instance_id = int(message.text.strip().split('#')[1])
        
        # Check if the instance exists and is currently borrowed
        borrowing = await borrowings_repo.get_borrowing_by_instance(db, instance_id)
        
        if not borrowing:
            await message.answer("❌ This book is not currently borrowed. Return process failed.")
            await state.clear()
            return
            
        if borrowing['user_id'] != message.from_user.id:
            await message.answer("❌ You don't borrow this book. Return process failed.")
            await state.clear()
            return
        if borrowing['state'] == 'pending':
            await message.answer("❌ This book is already pending. Return process failed.")
            await state.clear()
            return
            
        # Update borrowing state to returned
        await borrowings_repo.update_borrowing_state(db, borrowing['borrow_id'], 'pending')

        success_message = (
            f"✅  Return request sent\n\n"
            f"📚 {borrowing['title']}\n"
            f"👤 Author: {borrowing.get('author', 'Unknown')}\n"
            f"📖 Copy #{instance_id}\n"
            f"📅 Return by: {format_datetime(borrowing['borrow_return_time'])}\n\n"
            f"Thank you for returning the book! We will check it and approve the return soon."
        )
        
        await message.answer(success_message)
        
        # Notify admins about the return
        admins = await user_repo.get_admins()
        admin_message = (
           "Someone returned a book. Use /check to review the return"
        )
        for admin in admins:
            try:
                await bot.send_message(admin['id'], admin_message)
            except Exception as e:
                logging.error(f"Error sending message to admin {admin['id']}: {e}")
        
        await state.clear()
        
    except (ValueError, IndexError):
        await message.answer("❌ Please select a book from the keyboard or enter a valid instance ID (number).")
    except Exception as e:
        logging.error(f"Error in process_return_book: {e}")
        await message.answer("❌ An error occurred while processing the return. Please try again.")
        await state.clear()

async def handle_borrow(message: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Shared handler for borrow command and callback"""
    user = await user_repo.get_user(message.from_user.id)
    if user['confirm'] == False:
        text = "Please confirm your account first. Use /confirm command."
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)
        return

    borrowings = await borrowings_repo.get_user_borrowings(db, message.from_user.id)
    if len(borrowings) >= 3:
        text = "❌ You have reached the maximum number of borrowings (3)"
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)
        return

    text = "📚 Please enter the instance ID of the book you want to borrow.\nYou can find the instance ID on the book cover"
    if isinstance(message, Message):
        await message.answer(text)
    else:
        await message.message.answer(text)
    await state.set_state(BorrowState.SELECT_INSTANCE)

@dp.callback_query(F.data.startswith("action_borrow"))
async def borrow_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle borrow button click"""
    await handle_borrow(callback, state)

@dp.message(Command("borrow"))
async def command_borrow_handler(message: Message, state: FSMContext) -> None:
    """Handle /borrow command"""
    await handle_borrow(message, state)

@dp.message(BorrowState.SELECT_INSTANCE)
async def process_instance_selection(message: Message, state: FSMContext) -> None:
    """Process instance selection for borrowing"""
    if message.text.strip() == "/cancel":
            await message.answer("Borrowing process cancelled")
            await state.clear()
            return
        
    try:
        
        instance_id = int(message.text.strip())

        # Get instance details
        instance = await books_repo.get_instance_by_id(db, instance_id)
        if not instance:
            await message.answer("❌ Instance not found. Try again or use /cancel", show_alert=True)
            return
        
        if not instance['available']:
            await message.answer("❌ This copy is not available. Try again or use /cancel", show_alert=True)
            return
        
        
        # Create borrowing record
        borrow_id = await borrowings_repo.create_borrowing(db, message.from_user.id, instance_id)
        
        # Update instance availability
        await books_repo.update_book_availability(db, instance_id, False)
        
        # Get book details for success message
        books = await get_books(force_refresh=True)  # Refresh to get updated availability
        book = next((b for b in books if b['book_id'] == instance['book_id']), None)
        
        # Get borrowing details
        borrowing = await borrowings_repo.get_borrowing_by_instance(db, instance_id)
        return_time = borrowing['borrow_return_time'].strftime('%d %b %Y')
        
        success_message = (
            f"✅ Successfully borrowed\n\n"
            f"📚 {book['title']}\n"
            f"👤 Author: {book.get('author', 'Unknown')}\n"
            f"📖 Copy #{instance_id}\n"
            f"📅 Return by: {return_time}\n\n"
            f"Back to library /books"
        )
    
        await message.answer(success_message)
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Please enter a valid instance ID (number). Or /cancel")
    except Exception as e:
        logging.error(f"Error in process_instance_selection: {e}")
        await message.answer("❌ An error occurred while booking the copy. Please try again. Or /cancel")
        await state.clear()

async def handle_return(message: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Shared handler for return command and callback"""
    borrowings = await borrowings_repo.get_user_borrowings(db, message.from_user.id)
    if not borrowings:
        text = "You have no borrowings."
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)
        return
    
    text = "📚 Please select the book you want to return:"
    if isinstance(message, Message):
        await message.answer(text, reply_markup=create_return_keyboard(borrowings))
    else:
        await message.message.answer(text, reply_markup=create_return_keyboard(borrowings))
    await state.set_state(BorrowState.RETURN_BOOK)

@dp.callback_query(F.data.startswith("action_return"))
async def return_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle return button click"""
    await handle_return(callback, state)

@dp.message(Command("return"))
async def command_return_handler(message: Message, state: FSMContext) -> None:
    """Handle /return command"""
    await handle_return(message, state)

@dp.message(BorrowState.RETURN_BOOK)
async def process_return_book(message: Message, state: FSMContext, bot: Bot) -> None:
    """Process book return by instance ID"""
    try:
        # Extract instance ID from "Copy #id" format
        instance_id = int(message.text.strip().split('#')[1])
        
        # Check if the instance exists and is currently borrowed
        borrowing = await borrowings_repo.get_borrowing_by_instance(db, instance_id)
        
        if not borrowing:
            await message.answer("❌ This book is not currently borrowed. Return process failed.")
            await state.clear()
            return
            
        if borrowing['user_id'] != message.from_user.id:
            await message.answer("❌ You don't borrow this book. Return process failed.")
            await state.clear()
            return
        if borrowing['state'] == 'pending':
            await message.answer("❌ This book is already pending. Return process failed.")
            await state.clear()
            return
            
        # Update borrowing state to returned
        await borrowings_repo.update_borrowing_state(db, borrowing['borrow_id'], 'pending')

        success_message = (
            f"✅  Return request sent\n\n"
            f"📚 {borrowing['title']}\n"
            f"👤 Author: {borrowing.get('author', 'Unknown')}\n"
            f"📖 Copy #{instance_id}\n"
            f"📅 Return by: {format_datetime(borrowing['borrow_return_time'])}\n\n"
            f"Thank you for returning the book! We will check it and approve the return soon."
        )
        
        await message.answer(success_message)
        
        # Notify admins about the return
        admins = await user_repo.get_admins()
        admin_message = (
            f"📚 New book return request\n\n"
            f"Book: {borrowing['title']}\n"
            f"Author: {borrowing.get('author', 'Unknown')}\n"
            f"Copy #{instance_id}\n"
            f"Returned by: @{message.from_user.username}\n"
            f"Return by: {format_datetime(borrowing['borrow_return_time'])}\n\n"
            f"Use /check to review the return"
        )
        for admin in admins:
            try:
                await bot.send_message(admin['id'], admin_message)
            except Exception as e:
                logging.error(f"Error sending message to admin {admin['id']}: {e}")
        
        await state.clear()
        
    except (ValueError, IndexError):
        await message.answer("❌ Please select a book from the keyboard or enter a valid instance ID (number).")
    except Exception as e:
        logging.error(f"Error in process_return_book: {e}")
        await message.answer("❌ An error occurred while processing the return. Please try again.")
        await state.clear()

async def handle_history(message: Union[Message, CallbackQuery]) -> None:
    """Shared handler for history command and callback"""
    borrowings = await borrowings_repo.get_user_borrowings_history(db, message.from_user.id)
    if not borrowings:
        text = "You have no borrowings."
        if isinstance(message, Message):
            await message.answer(text)
        else:
            await message.message.answer(text)
        return

    text = "Your borrowing history:\n"
    for i, borrowing in enumerate(borrowings):
        text += f"{i+1}. Time: {format_datetime(borrowing['borrow_time'])}, "
        text += f"Copy #{borrowing['instance_id']}\n"
    
    if isinstance(message, Message):
        await message.answer(text)
    else:
        await message.message.answer(text)

@dp.callback_query(F.data.startswith("action_history"))
async def history_callback_handler(callback: CallbackQuery) -> None:
    """Handle history button click"""
    await handle_history(callback)

@dp.message(Command("history"))
async def command_history_handler(message: Message) -> None:
    """Handle /history command"""
    await handle_history(message)

@dp.message(Command("refresh_books"))   
async def command_refresh_books_handler(message: Message) -> None:
    user = await user_repo.get_user(message.from_user.id)
    if user['role'] != 'admin':
        await message.answer("You are not authorized to use this command.")
    else:
        await get_books(force_refresh=True)
        await message.answer("Books refreshed")

@dp.message(Command("check"))
async def command_check_handler(message: Message) -> None:
    """Handle /check command"""
    user = await user_repo.get_user(message.from_user.id)
    if user['role'] != 'admin':
        await message.answer("You are not authorized to use this command.")
    else:
        await message.answer("🔍 Checking...")
        pending_borrowings = await get_pending_borrowings()

        if not pending_borrowings:
            await message.answer("No pending borrowings")
            return
    
        await message.answer(
            f"📚 List of pending borrowings (Page 1):",
            reply_markup=create_pending_borrowings_keyboard(pending_borrowings, 0)
        )

ESP_IP = "192.168.1.184"

async def handle_open(message: Union[Message, CallbackQuery]) -> None:
    """Handler for communicating with an esp to control a light in the coworking space"""
    
    text = "Connecting..."
    if isinstance(message, Message):
        await message.answer(text)
    else:
        await message.message.answer(text)

    try:
        r = requests.get(f"http://{ESP_IP}/door", timeout=5)
        response = f"{r.text}"
    except Exception as e:
        response = f"\nFailed to reach the ESP: {e} \n Sorry for that"

    if isinstance(message, Message):
        await message.answer(response)
    else:
        await message.message.answer(response)

@dp.callback_query(F.data.startswith("action_open_sauna"))
async def open_callback_handler(callback: CallbackQuery) -> None:
    """Handle open button click"""
    await handle_open(callback)
@dp.message(Command("open"))
async def command_open_handler(message: Message) -> None:
    """Handle /open command"""
    await handle_open(message)

async def get_pending_borrowings(force_refresh: bool = False):
    global pending_borrowings
    if not pending_borrowings or force_refresh:
        pending_borrowings = await borrowings_repo.get_pending_borrowings(db)
    return pending_borrowings

@dp.callback_query(F.data.startswith("pending_page_"))
async def pending_page_handler(callback: CallbackQuery) -> None:
    """Handle pending borrowings pagination"""
    try:
        page = int(callback.data.split("_")[2])

        pending_borrowings = await get_pending_borrowings()
        
        
        if not pending_borrowings:
            await callback.answer("No pending borrowings", show_alert=True)
            return
            
        await callback.message.edit_text(
            f"📚 List of pending borrowings (Page {page + 1}):",
            reply_markup=create_pending_borrowings_keyboard(pending_borrowings, page)
        )
            
    except Exception as e:
        logging.error(f"Error in pending_page_handler: {e}")
        await callback.answer("An error occurred while changing pages", show_alert=True)

@dp.callback_query(F.data.startswith("pending_borrowing_"))
async def pending_borrowing_handler(callback: CallbackQuery) -> None:
    """Handle pending borrowing button clicks"""
    borrowing_id = int(callback.data.split("_")[2])
    page = int(callback.data.split("_")[3])
    borrowing = await borrowings_repo.get_borrowing_by_id(db, borrowing_id)
    text = f"📚 {borrowing['title']}\n"
    text += f"👤 Author: {borrowing['author']}\n"
    text += f"📖 Copy #{borrowing['instance_id']}\n"
    text += f"📅 Returned at: {format_datetime(borrowing['borrow_return_time'])}\n"
    text += f"Borrowed by: @{borrowing['username']}\n"

    await callback.message.edit_text(text, reply_markup=create_approve_borrowing_keyboard(borrowing_id, page))



@dp.callback_query(F.data.startswith("state_borrowing_"))
async def state_borrowing_handler(callback: CallbackQuery, bot: Bot) -> None:
    """Handle borrowing state button clicks"""
    borrowing_id = int(callback.data.split("_")[2])  # Convert to integer
    state = callback.data.split("_")[3]
    borrowing = await borrowings_repo.get_borrowing_by_id(db, borrowing_id)
    
    if state == "1":
        await borrowings_repo.update_borrowing_state(db, borrowing_id, 'returned')
        await books_repo.update_book_availability(db, borrowing['instance_id'], True)
        await bot.send_message(borrowing['user_id'], f"Admin has approved your return for Copy #{borrowing['instance_id']}")
    elif state == "0":
        if borrowing['borrow_return_time'] < datetime.now():
            await borrowings_repo.update_borrowing_state(db, borrowing_id, 'overdue')
        else:
            await borrowings_repo.update_borrowing_state(db, borrowing_id, 'borrowed')
        await bot.send_message(borrowing['user_id'], f"Admin hasn't approved your borrowing for Copy #{borrowing['instance_id']}")
    
    pending_borrowings = await get_pending_borrowings(force_refresh=True)
    if pending_borrowings:
        await callback.message.edit_text(
            f"📚 List of pending borrowings (Page 1):",
            reply_markup=create_pending_borrowings_keyboard(pending_borrowings, 0)
        )
    else:
        await callback.message.edit_text("No pending borrowings")

@dp.message(Command("menu"))
async def command_actions_handler(message: Message) -> None:
    """Handle /menu command"""
    keyboard = create_actions_keyboard(0)
    user = await user_repo.get_user(message.from_user.id)
    name = user['name'] if user['name'] else "@"+fmt_telegram(user['username'])
    await message.answer(f"Hello, {name}! What do you want to do?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("actions_page_"))
async def actions_page_handler(callback: CallbackQuery) -> None:
    """Handle actions pagination"""
    try:
        page = int(callback.data.split("_")[2])
        await callback.message.edit_text(
            "What do you want to do?",
            reply_markup=create_actions_keyboard(page)
        )
    except Exception as e:
        logging.error(f"Error in actions_page_handler: {e}")
        await callback.answer("An error occurred while changing pages", show_alert=True)

async def handle_apply(message: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Shared handler for apply command and callback"""
    if isinstance(message, Message):
        user = await user_repo.get_user(message.from_user.id)
    else:
        user = await user_repo.get_user(message.from_user.id)

    if user['confirm'] == False:
        if isinstance(message, Message):
            await message.answer("Please confirm your account first. Use /confirm command.", show_alert=True)
        else:
            await message.message.answer("Please confirm your account first. Use /confirm command.", show_alert=True)
        return

    # Check if user already has a pending request
    user_requests = await requests_repo.get_user_active_requests(db, message.from_user.id)
    user_access = await requests_repo.get_user_access(db, message.from_user.id)

    global document_file_id

    if user_access:
        if isinstance(message, Message):
            await message.answer(
                "❌ You already have Startup Sauna access. Check your access status with /access"
            )
        else:
            await message.message.answer(
                "❌ You already have Startup Sauna access. Check your access status with /access"
            )
        return

    if user_requests:
        if isinstance(message, Message):
            await message.answer(
                "❌ You already have a pending request for Startup Sauna access. "
                "Please wait for it to be reviewed before submitting a new one."
            )
        else:
            await message.message.answer(
                "❌ You already have a pending request for Startup Sauna access. "
                "Please wait for it to be reviewed before submitting a new one."
            )
        return
    
    try:
        text = "You are applying for Startup Sauna access. Please read document carefully and start application process."
        keyboard = create_apply_keyboard()
        if document_file_id:
            # Use cached file_id
            if isinstance(message, Message):
                await message.answer_document(
                    document=document_file_id,
                    caption=text,
                    reply_markup=keyboard
                )
            else:
                await message.message.answer_document(
                    document=document_file_id,
                    caption=text,
                    reply_markup=keyboard
                )
        else:
            # First time sending the document
            if isinstance(message, Message):
                sent_message = await message.answer_document(
                    document=FSInputFile("temp/Access to Startup Sauna.pdf"),
                    caption=text,
                    reply_markup=keyboard
                )
                # Store the file_id for future use
                document_file_id = sent_message.document.file_id
            else:
                sent_message = await message.message.answer_document(
                    document=FSInputFile("temp/Access to Startup Sauna.pdf"),
                    caption=text,
                    reply_markup=keyboard
                )
                # Store the file_id for future use
                document_file_id = sent_message.document.file_id
        
    except Exception as e:
        logging.error(f"Error sending document: {e}")
        if isinstance(message, Message):
            await message.answer(
                "❌ An error occurred while sending the document. "
                "Please try again later."
            )
        else:
            await message.message.answer(
                "❌ An error occurred while sending the document. "
                "Please try again later."
            )

@dp.callback_query(F.data.startswith("action_apply"))
async def apply_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle apply button click"""
    await handle_apply(callback, state)

@dp.message(Command("apply"))       
async def command_apply_handler(message: Message, state: FSMContext) -> None:
    """Handle /apply command"""
    await handle_apply(message, state)

@dp.callback_query(F.data.startswith("apply"))
async def process_apply(callback: CallbackQuery, state: FSMContext) -> None:
    """Process the apply for Startup Sauna access"""
    await callback.message.answer("Please provide a short description of why you want to work from Startup Sauna (max 500 characters). If you want to cancel, please send /cancel")
    await state.set_state(ApplyState.MOTIVATION)

@dp.message(ApplyState.MOTIVATION)
async def process_motivation(message: Message, state: FSMContext, bot: Bot) -> None:
    """Process the motivation state"""
    if message.text == "/cancel":
        await message.answer("Claim cancelled.")
        await state.clear()
        return
    
    logging.info(f"Motivation: {message.text}")
    
    await state.update_data(motivation=message.text)
    await message.answer("Please confirm your application", reply_markup=create_confirm_keyboard())
    await state.set_state(ApplyState.CONFIRMATION)

@dp.message(ApplyState.CONFIRMATION)
async def process_confirmation(message: Message, state: FSMContext, bot: Bot) -> None:
    """Process the confirmation for Startup Sauna access"""
    if message.text == "Cancel":
        await state.clear()
        await message.answer("Application cancelled.")
        return
    if message.text == "Confirm":
        # Get data from state before clearing it
        data = await state.get_data()

        if len(data.get('motivation')) > 500:
            await message.answer("❌ Error: Motivation is too long. Please send it again")
            await state.set_state(ApplyState.MOTIVATION)
            return
        
        motivation = data.get('motivation')
        
        if not motivation:
            await message.answer("❌ Error: Motivation not found. Please start the application process again.")
            await state.clear()
            return
            
        secret_word = secrets.token_hex(4).upper()

        try:
            await requests_repo.create_request(db, message.from_user.id, motivation, secret_word)
            await message.answer("Application sent. You can check the status with /access")
            admins = await user_repo.get_admins()
            for admin in admins:
                try:
                    await bot.send_message(admin['id'], f"New Startup Sauna access request from @{message.from_user.username}\n")
                except Exception as e:
                    logging.error(f"Error sending message to admin {admin['id']}: {e}")
        except Exception as e:
            logging.error(f"Error creating request: {e}")
            await message.answer("❌ An error occurred while submitting your application. Please try again.")
        finally:
            await state.clear()
        return
    
    await message.answer("Please confirm your application by sending Confirm or Cancel.")

@dp.message(Command("approve"))
async def command_review_requests_handler(message: Message) -> None:
    """Handle /approve command for admins"""

    current_requests, (pending, applied) = await get_requests(force_refresh=True)
    await message.answer(f"Current pending requests: {pending}\nNew application requests: {applied}", reply_markup=create_requests_keyboard(current_requests))
    
@dp.callback_query(F.data.startswith("requests_page_"))
async def requests_page_handler(callback: CallbackQuery) -> None:
    """Handle requests pagination"""
    try:
        page = int(callback.data.split("_")[2])
        
        all_requests, _ = await get_requests()
        
        if not all_requests:
            await callback.answer("No requests available", show_alert=True)
            return
            
        await callback.message.edit_text(
            f"📝 Requests (Page {page + 1}):",
            reply_markup=create_requests_keyboard(all_requests, page)
        )
            
    except Exception as e:
        logging.error(f"Error in requests_page_handler: {e}")
        await callback.answer("An error occurred while changing pages", show_alert=True)

@dp.callback_query(F.data.startswith("request_"))
async def request_handler(callback: CallbackQuery, bot: Bot) -> None:
    """Handle request button clicks"""
    request_id = int(callback.data.split("_")[1])
    page = int(callback.data.split("_")[2])
    request = await requests_repo.get_request_by_id(db, request_id)
    if request['state'] == 'applied':
        await bot.send_message(request['user_id'], f"Admin saw your request for Startup Sauna access.")
        await requests_repo.update_request_state(db, request_id, 'pending')
    user = await user_repo.get_user(request['user_id'])
    text = f"Request ID: {request['request_id']}\nName: {user['name']}\nUsername: @{user['username']}\n\nMotivation: \n{fmt_telegram(request['motivation'])}"
    await callback.message.edit_text(text, reply_markup=create_approve_request_keyboard(request_id, page))

@dp.callback_query(F.data.startswith("change_request_state_"))
async def change_request_state(callback: CallbackQuery, bot: Bot) -> None:
    """Handle request state change"""
    global current_requests
    request_id = int(callback.data.split("_")[3])
    state = int(callback.data.split("_")[4])
    request = await requests_repo.get_request_by_id(db, request_id)
    if state == 1:
        await requests_repo.update_request_state(db, request_id, 'approved')
        await bot.send_message(request['user_id'], f"Admin approved your request for Startup Sauna access. You can find your secret word in /access")

    else:
        await requests_repo.update_request_state(db, request_id, 'rejected')
        await bot.send_message(request['user_id'], f"Admin rejected your request for Startup Sauna access.")
    current_requests, (pending, applied) = await get_requests(force_refresh=True)
    await callback.message.edit_text(f"Current pending requests: {pending}\nNew application requests: {applied}", reply_markup=create_requests_keyboard(current_requests))

async def handle_access(message: Union[Message, CallbackQuery]) -> None:
    """Shared handler for access command and callback"""
    admin = await user_repo.get_user(message.from_user.id)
    if admin['role'] != 'admin':
        user = await requests_repo.get_user_last_request(db, message.from_user.id)
        if user['state'] == 'approved':
            text = f"You have access to Startup Sauna. Your secret word is \n\n*{user['secret_word']}*"
        elif user['state'] == 'applied':
            text = "Admin haven't seen your request yet."
        elif user['state'] == 'pending':
            text = "Admin has seen your request. Please wait for it to be reviewed."
        elif user['state'] == 'rejected':
            text = "Your request was rejected. Please try again later."
        else:
            text = "You do not have access to Startup Sauna and have no pending requests."

        if isinstance(message, Message):
            await message.answer(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    else:
        users = await get_users_with_access(force_refresh=True)
        if not users:
            if isinstance(message, Message):
                await message.answer("No users with access")
            else:
                await message.message.answer("No users with access")
        else:
            text = "Users with access (Page 1)"
            keyboard = create_users_keyboard(users)
            if isinstance(message, Message):
                await message.answer(text, reply_markup=keyboard)
            else:
                await message.message.answer(text, reply_markup=keyboard)

@dp.callback_query(F.data.startswith("action_access"))
async def access_callback_handler(callback: CallbackQuery) -> None:
    """Handle access button click"""
    await handle_access(callback)

@dp.message(Command("access"))
async def command_access_handler(message: Message) -> None:
    """Handle /access command"""
    await handle_access(message)

@dp.callback_query(F.data.startswith("user_"))
async def user_handler(callback: CallbackQuery) -> None:
    """Handle user button clicks"""
    user_id = int(callback.data.split("_")[1])
    page = int(callback.data.split("_")[2])
    user = await user_repo.get_user(user_id)
    request = await requests_repo.get_user_access(db, user_id)
    keyboard = create_update_user_keyboard(user, page)
    await callback.message.edit_text(f"Name: {user['name']}\nUsername: @{user['username']}\nSecret word: {request['secret_word']}\nJoined at: {format_datetime(user['created_at'])}", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("suspend_access_"))
async def suspend_access(callback: CallbackQuery, bot: Bot) -> None:
    """Handle suspend access button clicks"""
    user_id = int(callback.data.split("_")[2])
    await requests_repo.suspend_access(db, user_id)
    await bot.send_message(user_id, "Admin has suspended your access to Startup Sauna.")
    users = await get_users_with_access(force_refresh=True)
    if not users:
        await callback.message.edit_text("No users with access")
        return
    else:
            await callback.message.edit_text("Users with access (Page 1)", reply_markup=create_users_keyboard(users, 0))

@dp.callback_query(F.data.startswith("users_page_"))
async def users_page_handler(callback: CallbackQuery) -> None:
    """Handle users pagination"""
    try:
        page = int(callback.data.split("_")[2])
        
        # Get users with access
        users = await requests_repo.get_users_with_access(db)
        
        if not users:
            await callback.message.edit_text("No users with access")
            return
            
        await callback.message.edit_text(
            f"Users with access (Page {page + 1}):",
            reply_markup=create_users_keyboard(users, page)
        )
            
    except Exception as e:
        logging.error(f"Error in users_page_handler: {e}")
        await callback.answer("An error occurred while changing pages", show_alert=True)

async def handle_change_name(message: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Shared handler for change name command and callback"""
    text = "Please send your new full name. Example: John Doe\n\nYou can also send /cancel to cancel the name change."
    if isinstance(message, Message):
        await message.answer(text)
    else:
        await message.message.answer(text)
    await state.set_state(ChangeNameState.NEW_NAME)

@dp.callback_query(F.data.startswith("action_change_name"))
async def change_name_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle change name button click"""
    await handle_change_name(callback, state)

@dp.message(Command("change_name"))
async def command_change_name_handler(message: Message, state: FSMContext) -> None:
    """Handle /change_name command"""
    await handle_change_name(message, state)

@dp.message(ChangeNameState.NEW_NAME)
async def process_new_name(message: Message, state: FSMContext) -> None:
    """Process the new name state"""
    if message.text == "/cancel":
        await message.answer("Name change cancelled.")
        await state.clear()
    else:
        name = message.text
        await message.answer(f"Your full name is set to {name}.")
        await user_repo.update_user(message.from_user.id, name=name)
        await state.clear()


@dp.callback_query(F.data.startswith("task_"))
async def command_task_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    # Get the issue details
    parts = callback.data.split("_")
    team = parts[1]
    number = parts[2]

    issue = await get_issue_by_id(team, number)
    if not issue:
        await callback.message.edit_text(f"❌ Issue {team}-{number} not found.")
        return

    # Parse dates for formatting
    due_date = issue.get('due_date')
    # Format issue details
    details = (
        f"*{issue['team']}-{issue['number']}  {fmt_telegram(issue['title'])} ({issue['points']} pts)*\n\n"
        f"{format_priority(issue['priority'])}\n"
        f"📅 Due Date: {format_datetime(due_date) if due_date else 'No Due Date'}\n"
        f"📁 Project: {fmt_telegram(issue['project'])} {'(' + fmt_telegram(', '.join(issue['labels'])) + ')' if issue['labels'] else ''}\n\n"
        f"{fmt_telegram(issue['description'])}"
    )
    
    await callback.message.edit_text(details, parse_mode="Markdown", reply_markup=create_task_keyboard(issue))

@dp.message(Command("task"))
async def command_task_handler(message: Message) -> None:
    """Handle /task command to show Linear issue information"""
    # Get the issue ID from the command arguments

    user = await user_repo.get_user(message.from_user.id)
    if not user:
        user = await user_repo.save_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )

    if user['confirm'] == False:
        await message.answer("Please confirm your account first. Use /confirm command.", show_alert=True)
        return

    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "❌ Please provide an issue ID\n\n"
            "Usage:\n"
            "/task TSC-80"
        )
        return

    issue_ref = args[1]  # Format: TSC-80
    
    try:
        # Split the issue reference into team and number
        team, number = issue_ref.split("-")
        issue = await get_issue_by_id(team, number)
        if not issue:
            await message.answer(f"❌ Issue {issue_ref} not found.")
            return
    except ValueError:
        await message.answer(
            "❌ Invalid issue format.\n"
            "Please use format: TEAM-NUMBER\n"
            "Example: TSC-80"
        )
        return

    # Get the issue details


    # Parse dates for formatting
    due_date = issue.get('due_date')
    # Format issue details
    details = (
        f"*{issue['team']}-{issue['number']}  {fmt_telegram(issue['title'])} ({issue['points']} pts)*\n\n"
        f"{format_priority(issue['priority'])}\n"
        f"📅 Due Date: {format_datetime(due_date) if due_date else 'No Due Date'}\n"
        f"📁 Project: {fmt_telegram(issue['project'])} {'(' + fmt_telegram(', '.join(issue['labels'])) + ')' if issue['labels'] else ''}\n\n"
        f"{fmt_telegram(issue['description'])}"
    )
    
    await message.answer(details, parse_mode="Markdown", reply_markup=create_task_keyboard(issue))

@dp.callback_query(F.data.startswith("claim_task_"))
async def claim_task_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle claim task button click"""
    parts = callback.data.split("_")
    team = parts[2]
    number = parts[3]

    request = await points_requests_repo.get_user_request(db, callback.from_user.id, f"{team}-{number}")
    if request and request['state'] == 'approved':
        await callback.message.edit_text("You have already claimed this task.")
        return
    if request and request['state'] == 'pending':
        await callback.message.edit_text("You have already sent a request to claim this task. Please wait for the admin to review your request.")
        return
    # Store task info in state
    await state.update_data(team=team, number=number)

    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back", callback_data=f"task_{team}_{number}")]
    ])
    await callback.message.edit_text(
        "You are about to claim the task. Please send your motivation. "
        "Please explain what you have done and why you deserve the points. "
        "You can also send /cancel to cancel the claim.", 
        reply_markup=back_keyboard
    )
    await state.set_state(ClaimTaskState.MOTIVATION)

@dp.message(ClaimTaskState.MOTIVATION)
async def process_motivation(message: Message, state: FSMContext, bot: Bot) -> None:
    """Process the motivation state"""
    if message.text == "/cancel":
        await message.answer("Claim cancelled.")
        await state.clear()
        return

    # Get data from state
    data = await state.get_data()
    if not data or 'team' not in data or 'number' not in data:
        await message.answer("❌ Error: Task information not found. Please try claiming the task again.")
        await state.clear()
        return

    team = data['team']
    number = data['number']
    motivation = message.text

    try:
        # Get issue details to get points
        issue = await get_issue_by_id(team, number)
        if not issue:
            await message.answer("❌ Error: Task not found. Please try again.")
            await state.clear()
            return

        # Create points request
        issue_id = f"{team}-{number}"
        await points_requests_repo.create_request(
            db,
            user_id=message.from_user.id,
            issue_id=issue_id,
            points=int(issue['points']),
            motivation=motivation
        )

        await message.answer(
            f"✅ Points request submitted!\n\n"
            f"Task: {team}-{number}\n"
            f"Points: {issue['points']}\n\n"
            f"We will review your request and get back to you soon."
        )

           # Notify admins
        admins = await user_repo.get_admins()
        admin_message = (
            f"🎯 New points request from @{message.from_user.username}\n\n"
            f"Use /points to review the request"
        )
        
        for admin in admins:
            try:
                await bot.send_message(admin['id'], admin_message)
            except Exception as e:
                logging.error(f"Error sending message to admin {admin['id']}: {e}")


        await state.clear()

    except Exception as e:
        logging.error(f"Error creating points request: {e}")
        await message.answer("❌ An error occurred while submitting your request. Please try again.")
        await state.clear()

@dp.message(Command("points"))
async def command_points_handler(message: Message) -> None:
    """Handle /points command for admins"""
    user = await user_repo.get_user(message.from_user.id)
    if user['role'] != 'admin':
        await message.answer("You are not authorized to use this command.")
        return

    requests = await points_requests_repo.get_requests(db)
    if not requests:
        await message.answer("No points requests available.")
        return

    await message.answer(
        f"📊 Points Requests (Page 1):",
        reply_markup=create_points_requests_keyboard(requests, 0)
    )

@dp.callback_query(F.data.startswith("points_page_"))
async def points_page_handler(callback: CallbackQuery) -> None:
    """Handle points requests pagination"""
    try:
        page = int(callback.data.split("_")[2])
        
        requests = await points_requests_repo.get_requests(db)
        if not requests:
            await callback.answer("No points requests available", show_alert=True)
            return
            
        await callback.message.edit_text(
            f"📊 Points Requests (Page {page + 1}):",
            reply_markup=create_points_requests_keyboard(requests, page)
        )
            
    except Exception as e:
        logging.error(f"Error in points_page_handler: {e}")
        await callback.answer("An error occurred while changing pages", show_alert=True)

@dp.callback_query(F.data.startswith("points_request_"))
async def points_request_handler(callback: CallbackQuery, bot: Bot) -> None:
    """Handle points request button clicks"""
    request_id = int(callback.data.split("_")[2])
    page = int(callback.data.split("_")[3])
    
    request = await points_requests_repo.get_request_by_id(db, request_id)
    if not request:
        await callback.answer("Request not found", show_alert=True)
        return

    user = await user_repo.get_user(request['user_id'])
    # Format the issue ID as a proper Markdown link
    issue_link = f"[{request['issue_id']}](https://linear.app/aaltoes25/issue/{request['issue_id']})"
    
    text = (
        f"🎯 Points Request\n\n"
        f"From: @{fmt_telegram(user['username'])}\n"
        f"Task: {issue_link}\n"
        f"Points: {request['points']}\n\n"
        f"Motivation:\n{fmt_telegram(request['motivation'])}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=create_approve_points_request_keyboard(request_id, page),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("change_points_state_"))
async def change_points_state(callback: CallbackQuery, bot: Bot) -> None:
    """Handle points request state change"""
    request_id = int(callback.data.split("_")[3])
    state = int(callback.data.split("_")[4])
    page = int(callback.data.split("_")[5])
    
    request = await points_requests_repo.get_request_by_id(db, request_id)
    if not request:
        await callback.answer("Request not found", show_alert=True)
        return

    try:
        if state == 1:  # Approve
            await points_requests_repo.update_request_state(db, request_id, 'approved')
            await points_repo.add_points(db, request['user_id'], request['points'])
            await user_repo.update_points(request['user_id'], request['points'])
            await bot.send_message(
                request['user_id'],
                f"✅ Your points request for {request['issue_id']} has been approved!\n"
                f"You received {request['points']} points."
            )
        else:  # Reject
            await points_requests_repo.update_request_state(db, request_id, 'rejected')
            await bot.send_message(
                request['user_id'],
                f"❌ Your points request for {request['issue_id']} has been rejected."
            )

        # Refresh requests list
        requests = await points_requests_repo.get_requests(db)
        if requests:
            await callback.message.edit_text(
                f"📊 Points Requests (Page {page + 1}):",
                reply_markup=create_points_requests_keyboard(requests, page)
            )
        else:
            await callback.message.edit_text("No points requests available.")
            
    except Exception as e:
        logging.error(f"Error changing points request state: {e}")
        await callback.answer("An error occurred while processing the request", show_alert=True)
@dp.message(Command('profile'))
async def command_profile_handler(message: Message) -> None:
    """Handle /profile command"""
    user = await user_repo.get_user(message.from_user.id)
    if not user:
        await message.answer("You are not registered in the system.")
        return
    else:
        await message.answer(
                             f"Name: {user['name']}\n"
                             f"Points: {user['points']}\n"
                             f"Role: {user['role']}\n"
        )

@dp.message()
async def handle_unknown_message(message: Message) -> None:
    """Handle unknown messages"""
    await message.answer(choice(phrases))

async def main() -> None:
    
    # Initialize Bot instance with default bot properties which will be passed to all API calls
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Connect to the database
    await db.connect(settings.DATABASE_URL_UNPOOLED)
    # Initialize tables
    await user_repo.init(db)
    await reminders_repo.init(db)
    await books_repo.init(db)
    await borrowings_repo.init(db)
    await requests_repo.init(db)
    await points_requests_repo.init(db)
    await points_repo.init(db)

    # Initialize events
    global current_events
    await refresh_events()
    if current_events:
        logging.info(f"Events refreshed at {datetime.now()}")

    # Initialize reminder tasks for all existing reminders
    global reminder_tasks
   
    #delete unnecessary reminders that are expired
    all_reminders = await reminders_repo.get_reminders(db)
    for reminder in all_reminders:
        event = current_events.get(reminder['event_id'])

        if not event:
            await reminders_repo.delete_reminder(db, reminder['user_id'], reminder['event_id'])
            continue

        if event['start_time'].astimezone(pytz.utc) < datetime.now().astimezone(pytz.utc):
            await reminders_repo.delete_reminder(db, reminder['user_id'], reminder['event_id'])
        else:
            event = current_events.get(reminder['event_id'])
            if event:
                task = asyncio.create_task(schedule_reminder(bot, reminder['user_id'], event))
                task_key = f"reminder_{event['id']}_{reminder['user_id']}"
                reminder_tasks[task_key] = task
                logging.info(f"Initialized reminder task for event {event['id']} and user {reminder['user_id']}")
            else:
                await reminders_repo.delete_reminder(db, reminder['user_id'], reminder['event_id'])
    
    # Create periodic tasks
    periodic_tasks = [
        periodic_events_refresh(),
        update_overdue_borrowings(bot),
        send_overdue_notification(bot)
    ]
    
    # Start periodic tasks and bot polling concurrently
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            *periodic_tasks
        )
    finally:
        # Close the database connection when the bot is stopped
        await db.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())