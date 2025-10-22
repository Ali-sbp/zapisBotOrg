#!/usr/bin/env python3
"""
University Course Registration Queue Bot
A Telegram bot for managing course registration queues with time-based opening.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, time, timedelta
from typing import Dict, List, Set
from collections import defaultdict

import pytz
from dotenv import load_dotenv
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Chat
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    JobQueue,
    filters
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO if os.getenv('DEBUG', 'False').lower() == 'true' else logging.WARNING
)
logger = logging.getLogger(__name__)

# Bot configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Dev User Configuration - Load from environment variables for security
DEV_USER_IDS_ENV = os.getenv('DEV_USER_IDS', '')
DEV_USER_IDS = []
if DEV_USER_IDS_ENV:
    try:
        DEV_USER_IDS = [int(user_id.strip()) for user_id in DEV_USER_IDS_ENV.split(',') if user_id.strip()]
    except ValueError:
        logger.error("Invalid DEV_USER_IDS format in environment variables. Expected comma-separated integers.")

REGISTRATION_DAY = int(os.getenv('REGISTRATION_DAY', 2))  # Wednesday = 2
REGISTRATION_TIME = os.getenv('REGISTRATION_TIME', '20:00')
TIMEZONE = pytz.timezone('Europe/Moscow')  # Adjust to your university's timezone

# Data storage (in production, use a proper database)
class QueueManager:
    def __init__(self):
        self.data_file = 'queue_data.json'
        self.config_file = 'config.json'
        
        # Group-aware data structures
        self.groups: Dict[int, Dict] = {}  # group_id -> group_info
        self.group_queues: Dict[int, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))  # group_id -> course_id -> queue
        self.group_registration_status: Dict[int, Dict[str, bool]] = defaultdict(dict)  # group_id -> course_id -> status
        self.group_courses: Dict[int, Dict] = defaultdict(dict)  # group_id -> course_id -> course_name
        self.group_schedules: Dict[int, Dict] = defaultdict(dict)  # group_id -> course_id -> schedule
        self.user_groups: Dict[int, int] = {}  # user_id -> associated_group_id (for private message context)
        
        # Global admin configuration
        self.dev_users = []    # Global dev users (full access)
        self.group_admins = defaultdict(list)  # group_id -> [admin_user_ids]
        self.max_queue_size = 50  # Global default (fallback)
        self.group_queue_sizes = defaultdict(lambda: 50)  # group_id -> queue_size
        self.blacklist = []  # List of user IDs that are blacklisted from registration
        
        # Load configuration first
        self.load_config()
        # Then load queue data
        self.load_data()
        
        # Migration from old single-group format
        self._migrate_legacy_data()
    
    def load_config(self):
        """Load configuration from config.json with duplicate key detection"""
        try:
            # First, read the raw file content to detect duplicate keys
            with open(self.config_file, 'r', encoding='utf-8') as f:
                raw_content = f.read()
                
            # Check for duplicate keys in group_queue_sizes section
            import re
            if 'group_queue_sizes' in raw_content:
                # Find the group_queue_sizes section
                queue_sizes_match = re.search(r'"group_queue_sizes"\s*:\s*\{([^}]*)\}', raw_content)
                if queue_sizes_match:
                    queue_sizes_content = queue_sizes_match.group(1)
                    # Find all keys in the group_queue_sizes section
                    key_matches = re.findall(r'"(-?\d+)"\s*:', queue_sizes_content)
                    unique_keys = set(key_matches)
                    if len(key_matches) != len(unique_keys):
                        logger.warning(f"Duplicate keys detected in group_queue_sizes: {key_matches}")
                        logger.info("Will attempt to load and fix duplicate keys during save")
            
            # Now load the JSON normally
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                
                # Load admin users and global settings
                # Only load dev_users from config if no environment dev users are set
                if not DEV_USER_IDS:
                    self.dev_users = config.get('dev_users', [])      # Fallback to config-based dev users
                else:
                    self.dev_users = []  # Use environment-based dev users instead
                    logger.info(f"Using {len(DEV_USER_IDS)} dev users from environment variables")
                    
                self.group_admins = defaultdict(list, config.get('group_admins', {}))  # Per-group admins
                self.max_queue_size = config.get('max_queue_size', 50)  # Global default
                
                # Load blacklist
                self.blacklist = config.get('blacklist', [])
                logger.info(f"Loaded {len(self.blacklist)} blacklisted users")
                
                # Load group_queue_sizes with duplicate key handling
                raw_group_queue_sizes = config.get('group_queue_sizes', {})
                self.group_queue_sizes = defaultdict(lambda: 50)
                for group_id, size in raw_group_queue_sizes.items():
                    # Convert group_id to int for internal consistency
                    try:
                        int_group_id = int(group_id)
                        self.group_queue_sizes[int_group_id] = size
                        logger.debug(f"Loaded queue size for group {int_group_id}: {size}")
                    except ValueError:
                        logger.warning(f"Invalid group ID in group_queue_sizes: {group_id}")
                
                # Load groups configuration (new format)
                groups_config = config.get('groups', {})
                
                # Handle migration from old format
                if not groups_config and 'courses' in config:
                    # Old format: single group, migrate to new format
                    logger.info("Migrating old config format to group-aware format...")
                    default_group_id = -1001234567890  # Use a default group ID for migration
                    groups_config[str(default_group_id)] = {
                        'name': 'Default Group',
                        'courses': config.get('courses', {})
                    }
                
                # Load group data
                for group_id_str, group_data in groups_config.items():
                    try:
                        group_id = int(group_id_str)
                        self.groups[group_id] = {
                            'name': group_data.get('name', f'Group {group_id}'),
                            'created_at': group_data.get('created_at', datetime.now().isoformat())
                        }
                        
                        # Load courses for this group
                        courses_config = group_data.get('courses', {})
                        for course_id, course_data in courses_config.items():
                            if isinstance(course_data, str):
                                # Old format: course_data is just the name
                                self.group_courses[group_id][course_id] = course_data
                                self.group_schedules[group_id][course_id] = {"day": 2, "time": "20:00"}
                            elif isinstance(course_data, dict):
                                # New format: course_data has name and schedule
                                self.group_courses[group_id][course_id] = course_data.get('name', course_id)
                                self.group_schedules[group_id][course_id] = course_data.get('schedule', {"day": 2, "time": "20:00"})
                            else:
                                # Fallback
                                self.group_courses[group_id][course_id] = str(course_data)
                                self.group_schedules[group_id][course_id] = {"day": 2, "time": "20:00"}
                            
                            # Initialize registration as closed
                            self.group_registration_status[group_id][course_id] = False
                            
                    except (ValueError, TypeError) as e:
                        logger.error(f"Invalid group ID '{group_id_str}': {e}")
                        continue
                
                # If no groups loaded, create default configuration
                if not self.groups:
                    logger.info("No groups found in config, creating default configuration")
                    self._create_default_group_config()
                
                logger.info(f"Loaded config: {len(self.groups)} groups, {len(self.dev_users)} devs, {len(self.group_admins)} group admin entries")
                
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            self._create_default_group_config()
    
    def _create_default_group_config(self):
        """Create default configuration for migration or first run"""
        default_group_id = -1001234567890  # Default group ID for single-group setups
        self.groups[default_group_id] = {
            'name': 'Default Group',
            'created_at': datetime.now().isoformat()
        }
        
        # Default courses
        default_courses = {
            'oop_lab': 'ООП Лаб',
            'cvm_lab': 'ЦВМ Лаб', 
            'discrete': 'Дискретка'
        }
        
        for course_id, course_name in default_courses.items():
            self.group_courses[default_group_id][course_id] = course_name
            self.group_schedules[default_group_id][course_id] = {"day": 2, "time": "20:00"}
            self.group_registration_status[default_group_id][course_id] = False
    
    def save_data(self):
        """Save queue data to file"""
        try:
            data = {
                'group_queues': {str(group_id): dict(queues) for group_id, queues in self.group_queues.items()},
                'group_registration_status': {str(group_id): dict(status) for group_id, status in self.group_registration_status.items()},
                'user_groups': {str(user_id): group_id for user_id, group_id in self.user_groups.items()},
                'last_updated': datetime.now().isoformat(),
                'format_version': '2.0'  # Mark as new format
            }
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving data: {e}")
    
    def load_data(self):
        """Load queue data from file"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    
                    # Check format version
                    format_version = data.get('format_version', '1.0')
                    
                    if format_version == '2.0':
                        # New group-aware format
                        self._load_group_data(data)
                    else:
                        # Old format - will be handled by migration
                        self._legacy_data = data
                    
                    logger.info(f"Loaded data format {format_version}")
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    
    def _load_group_data(self, data):
        """Load data in new group-aware format"""
        # Load group queues
        group_queues_data = data.get('group_queues', {})
        for group_id_str, queues in group_queues_data.items():
            try:
                group_id = int(group_id_str)
                self.group_queues[group_id] = defaultdict(list, queues)
            except (ValueError, TypeError):
                logger.error(f"Invalid group ID in queue data: {group_id_str}")
        
        # Load group registration status
        group_status_data = data.get('group_registration_status', {})
        for group_id_str, status in group_status_data.items():
            try:
                group_id = int(group_id_str)
                self.group_registration_status[group_id] = dict(status)
            except (ValueError, TypeError):
                logger.error(f"Invalid group ID in status data: {group_id_str}")
        
        # Load user-group associations
        user_groups_data = data.get('user_groups', {})
        for user_id_str, group_id in user_groups_data.items():
            try:
                user_id = int(user_id_str)
                self.user_groups[user_id] = group_id
            except (ValueError, TypeError):
                logger.error(f"Invalid user ID in association data: {user_id_str}")
        
        # Ensure all groups have registration status for all their courses
        for group_id, courses in self.group_courses.items():
            for course_id in courses:
                if course_id not in self.group_registration_status[group_id]:
                    self.group_registration_status[group_id][course_id] = False
    
    def _migrate_legacy_data(self):
        """Migrate data from old single-group format to new group-aware format"""
        if not hasattr(self, '_legacy_data'):
            return
        
        logger.info("Migrating legacy queue data to group-aware format...")
        
        try:
            legacy_data = self._legacy_data
            default_group_id = list(self.groups.keys())[0] if self.groups else -1001234567890
            
            # Migrate queues
            old_queues = legacy_data.get('queues', {})
            for course_id, queue in old_queues.items():
                if course_id in self.group_courses.get(default_group_id, {}):
                    self.group_queues[default_group_id][course_id] = queue
            
            # Migrate registration status
            old_status = legacy_data.get('course_registration_status', {})
            for course_id, status in old_status.items():
                if course_id in self.group_courses.get(default_group_id, {}):
                    self.group_registration_status[default_group_id][course_id] = status
            
            # Handle old global registration_open flag
            if 'registration_open' in legacy_data and not old_status:
                global_status = legacy_data['registration_open']
                for course_id in self.group_courses.get(default_group_id, {}):
                    self.group_registration_status[default_group_id][course_id] = global_status
            
            # Save migrated data
            self.save_data()
            logger.info("Legacy data migration completed")
            
            # Clean up
            del self._legacy_data
            
        except Exception as e:
            logger.error(f"Error migrating legacy data: {e}")
            if hasattr(self, '_legacy_data'):
                del self._legacy_data
        
    # BACKWARD COMPATIBILITY PROPERTIES
    @property 
    def courses(self):
        """Backward compatibility: return courses from default group"""
        default_group = list(self.groups.keys())[0] if self.groups else None
        if default_group:
            return self.group_courses.get(default_group, {})
        return {}
    
    @property
    def course_schedules(self):
        """Backward compatibility: return schedules from default group"""
        default_group = list(self.groups.keys())[0] if self.groups else None
        if default_group:
            return self.group_schedules.get(default_group, {})
        return {}
    
    @property 
    def queues(self):
        """Backward compatibility: return queues from default group"""
        default_group = list(self.groups.keys())[0] if self.groups else None
        if default_group:
            return self.group_queues.get(default_group, defaultdict(list))
        return defaultdict(list)
    
    @property
    def course_registration_status(self):
        """Backward compatibility: return registration status from default group"""
        default_group = list(self.groups.keys())[0] if self.groups else None
        if default_group:
            return self.group_registration_status.get(default_group, {})
        return {}
    
    # Backward compatibility methods for single-group operations
    def get_course_registration_status_compat(self, course_id: str) -> str:
        """Backward compatibility version"""
        default_group = list(self.groups.keys())[0] if self.groups else None
        if default_group:
            return self.get_course_registration_status(default_group, course_id)
        return "🔴 Closed"
    
    def is_course_registration_open_compat(self, course_id: str) -> bool:
        """Backward compatibility version"""
        default_group = list(self.groups.keys())[0] if self.groups else None
        if default_group:
            return self.is_course_registration_open(default_group, course_id)
        return False
    
    def get_user_group(self, user_id: int) -> int | None:
        """Get the associated group for a user (for private message context)"""
        return self.user_groups.get(user_id)
    
    def associate_user_with_group(self, user_id: int, group_id: int):
        """Associate a user with a group for private message context"""
        self.user_groups[user_id] = group_id
        self.save_data()
        logger.info(f"Associated user {user_id} with group {group_id}")
    
    def initialize_group(self, group_id: int, group_name: str = None):
        """Initialize a new group when bot is added to it"""
        if group_id in self.groups:
            logger.info(f"Group {group_id} already initialized")
            return
        
        # Create group info
        self.groups[group_id] = {
            'name': group_name or f'Group {group_id}',
            'created_at': datetime.now().isoformat()
        }
        
        # Initialize with default courses
        default_courses = {
            'oop_lab': 'ООП Лаб',
            'cvm_lab': 'ЦВМ Лаб', 
            'discrete': 'Дискретка'
        }
        
        for course_id, course_name in default_courses.items():
            self.group_courses[group_id][course_id] = course_name
            self.group_schedules[group_id][course_id] = {"day": 2, "time": "20:00"}
            self.group_registration_status[group_id][course_id] = False
            self.group_queues[group_id][course_id] = []
        
        # Save changes
        self.save_config()
        self.save_data()
        
        logger.info(f"Initialized new group {group_id} ({group_name}) with {len(default_courses)} default courses")
    
    def add_to_queue(self, group_id: int, course_id: str, user_id: int, user_name: str, full_name: str) -> tuple[bool, str]:
        """Add user to course queue for a specific group"""
        # Check if user is blacklisted
        if user_id in self.blacklist:
            return False, "Sorry an error occured. Try again later and don't spam!"
        
        # Validate group exists
        if group_id not in self.groups:
            return False, "Group not found! Bot may need to be re-added to the group."
        
        # Check if registration is open
        if not self.group_registration_status.get(group_id, {}).get(course_id, False):
            course_name = self.group_courses.get(group_id, {}).get(course_id, course_id)
            return False, f"Registration for {course_name} is currently closed!"
        
        # Validate course exists in this group
        if course_id not in self.group_courses.get(group_id, {}):
            return False, "Invalid course selected!"
        
        # Check if this name is already registered for this course in this group
        for entry in self.group_queues[group_id][course_id]:
            if entry['full_name'].lower() == full_name.lower():
                registered_by = entry['username'] if entry['username'] != "Unknown" else f"User {entry['user_id']}"
                course_name = self.group_courses[group_id][course_id]
                return False, f"Имя '{full_name}' уже записано на {course_name} пользователем @{registered_by}!"
        
        # Check queue size limit
        max_size = self.get_group_queue_size(group_id)
        if len(self.group_queues[group_id][course_id]) >= max_size:
            return False, f"Очередь полная! Максимум {max_size} записей разрешено."
        
        # Add user to queue
        entry = {
            'user_id': user_id,
            'username': user_name,
            'full_name': full_name,
            'registered_at': datetime.now(TIMEZONE).isoformat(),
            'position': len(self.group_queues[group_id][course_id]) + 1
        }
        
        self.group_queues[group_id][course_id].append(entry)
        self.save_data()
        
        course_name = self.group_courses[group_id][course_id]
        return True, f"Успешно записали '{full_name}' на {course_name}! Позиция: {entry['position']}"
    
    def get_queue_status(self, group_id: int, course_id: str = None) -> str:
        """Get queue status for a course or all courses in a specific group"""
        # Validate group exists
        if group_id not in self.groups:
            return "Group not found! Bot may need to be re-added to the group."
        
        if course_id:
            if course_id not in self.group_courses.get(group_id, {}):
                return "Invalid course!"
            
            queue = self.group_queues[group_id][course_id]
            if not queue:
                course_name = self.group_courses[group_id][course_id]
                return f"{course_name}: No registrations yet"
            
            course_name = self.group_courses[group_id][course_id]
            status = f"📚 {course_name} ({len(queue)} registered):\n"
            for i, entry in enumerate(queue[:10], 1):  # Show top 10
                status += f"{i}. {entry['full_name']} (@{entry['username']})\n"
            
            if len(queue) > 10:
                status += f"... and {len(queue) - 10} more"
            
            return status
        
        # Show all courses for this group
        group_courses = self.group_courses.get(group_id, {})
        if not group_courses:
            return "No courses configured for this group."
        
        if not any(self.group_queues[group_id].values()):
            return "No registrations in any course yet."
        
        group_name = self.groups[group_id]['name']
        status = f"📋 **Current Registration Status - {group_name}:**\n\n"
        for course_id, course_name in group_courses.items():
            count = len(self.group_queues[group_id][course_id])
            status += f"📚 **{course_name}**: {count} registered\n"
        
        return status
    
    def clear_queues(self, group_id: int):
        """Clear all queues for a specific group"""
        if group_id in self.groups:
            self.group_queues[group_id].clear()
            self.save_data()
    
    def clear_course_queue(self, group_id: int, course_id: str):
        """Clear queue for a specific course in a specific group"""
        if group_id in self.groups and course_id in self.group_courses.get(group_id, {}):
            self.group_queues[group_id][course_id] = []
            self.save_data()
    
    def open_course_registration(self, group_id: int, course_id: str):
        """Open registration for a specific course in a specific group"""
        if group_id in self.groups and course_id in self.group_courses.get(group_id, {}):
            self.group_registration_status[group_id][course_id] = True
            self.save_data()
    
    def close_course_registration(self, group_id: int, course_id: str):
        """Close registration for a specific course in a specific group"""
        if group_id in self.groups and course_id in self.group_courses.get(group_id, {}):
            self.group_registration_status[group_id][course_id] = False
            self.save_data()
    
    def set_course_registration_status(self, group_id: int, course_id: str, status: bool):
        """Set registration status for a specific course in a specific group"""
        if group_id in self.groups and course_id in self.group_courses.get(group_id, {}):
            self.group_registration_status[group_id][course_id] = status
            self.save_data()
    
    def is_course_registration_open(self, group_id: int, course_id: str) -> bool:
        """Check if registration is open for a specific course in a specific group"""
        return self.group_registration_status.get(group_id, {}).get(course_id, False)
    
    def get_course_registration_status(self, group_id: int, course_id: str) -> str:
        """Get formatted registration status for a course in a specific group"""
        is_open = self.group_registration_status.get(group_id, {}).get(course_id, False)
        return "🟢 Open" if is_open else "🔴 Closed"
    
    def open_registration(self, group_id: int):
        """Open registration for ALL courses in a specific group"""
        if group_id in self.groups:
            for course_id in self.group_courses.get(group_id, {}):
                self.group_registration_status[group_id][course_id] = True
            self.save_data()
    
    def close_registration(self, group_id: int):
        """Close registration for ALL courses in a specific group"""
        if group_id in self.groups:
            for course_id in self.group_courses.get(group_id, {}):
                self.group_registration_status[group_id][course_id] = False
            self.save_data()
    
    def get_group_courses(self, group_id: int) -> dict:
        """Get all courses for a specific group"""
        return self.group_courses.get(group_id, {})
    
    def get_group_schedules(self, group_id: int) -> dict:
        """Get all course schedules for a specific group"""
        return self.group_schedules.get(group_id, {})
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin (legacy method for backward compatibility)"""
        # Legacy global admins are deprecated - always return False
        return False
    
    def is_dev(self, user_id: int) -> bool:
        """Check if user is a dev (global access)"""
        # Check against environment-based dev user IDs and config-based dev users
        return user_id in DEV_USER_IDS or user_id in self.dev_users
    
    def is_group_admin(self, user_id: int, group_id) -> bool:
        """Check if user is admin for a specific group"""
        # Convert group_id to string to match JSON format  
        group_key = str(group_id)
        return user_id in self.group_admins.get(group_key, [])
    
    def has_admin_access(self, user_id: int, group_id = None) -> bool:
        """Check if user has admin access (dev, legacy admin, or group admin)"""
        # Dev users have global access
        if self.is_dev(user_id):
            return True
        
        # Legacy global admins have access to everything
        if self.is_admin(user_id):
            return True
        
        # If checking for a specific group, check group admin access
        if group_id is not None:
            return self.is_group_admin(user_id, group_id)
        
        # If no specific group, check if user is admin for any group
        for gid in self.groups.keys():
            if self.is_group_admin(user_id, gid):
                return True
        
        return False
    
    def add_to_blacklist(self, user_id: int) -> tuple[bool, str]:
        """Add a user to the blacklist (dev only)"""
        if user_id in self.blacklist:
            return False, f"User {user_id} is already blacklisted."
        
        self.blacklist.append(user_id)
        self.save_config()
        return True, f"✅ User {user_id} has been added to the blacklist."
    
    def remove_from_blacklist(self, user_id: int) -> tuple[bool, str]:
        """Remove a user from the blacklist (dev only)"""
        if user_id not in self.blacklist:
            return False, f"User {user_id} is not in the blacklist."
        
        self.blacklist.remove(user_id)
        self.save_config()
        return True, f"✅ User {user_id} has been removed from the blacklist."
    
    def is_blacklisted(self, user_id: int) -> bool:
        """Check if a user is blacklisted"""
        return user_id in self.blacklist
    
    def get_blacklist(self) -> list[int]:
        """Get the full blacklist"""
        return self.blacklist.copy()
    
    def get_admin_groups(self, user_id: int) -> list[str]:
        """Get list of group IDs where the user is an admin"""
        admin_groups = []
        for group_id, admin_ids in self.group_admins.items():
            if user_id in admin_ids:
                admin_groups.append(group_id)
        return admin_groups
    
    def get_group_queue_size(self, group_id: int) -> int:
        """Get the queue size limit for a specific group"""
        return self.group_queue_sizes.get(group_id, self.max_queue_size)
    
    def set_group_queue_size(self, group_id: int, size: int) -> bool:
        """Set the queue size limit for a specific group"""
        if size <= 0:
            return False
        self.group_queue_sizes[group_id] = size
        self.save_config()
        return True
    
    def save_config(self):
        """Save configuration to config.json with validation and atomic write"""
        try:
            # Ensure group_queue_sizes is properly formatted (no duplicate keys)
            cleaned_group_queue_sizes = {}
            for group_id, size in self.group_queue_sizes.items():
                # Convert to string key for consistency
                key = str(group_id)
                cleaned_group_queue_sizes[key] = size
            
            config = {
                'groups': {},
                'dev_users': self.dev_users,          # New dev users
                'group_admins': dict(self.group_admins),  # Per-group admins
                'max_queue_size': self.max_queue_size,  # Global default
                'group_queue_sizes': cleaned_group_queue_sizes,  # Per-group queue sizes
                'blacklist': self.blacklist  # Blacklisted user IDs
            }
            
            # Save group configurations
            for group_id, group_info in self.groups.items():
                group_courses = {}
                for course_id, course_name in self.group_courses.get(group_id, {}).items():
                    group_courses[course_id] = {
                        'name': course_name,
                        'schedule': self.group_schedules.get(group_id, {}).get(course_id, {"day": 2, "time": "20:00"})
                    }
                
                config['groups'][str(group_id)] = {
                    'name': group_info['name'],
                    'created_at': group_info['created_at'],
                    'courses': group_courses
                }
            
            # Atomic write: write to temporary file first, then rename
            temp_config_file = self.config_file + '.tmp'
            with open(temp_config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            # Validate the written JSON by trying to parse it
            with open(temp_config_file, 'r', encoding='utf-8') as f:
                test_config = json.load(f)
            
            # If validation passes, replace the original file
            import os
            if os.path.exists(self.config_file):
                os.replace(temp_config_file, self.config_file)
            else:
                os.rename(temp_config_file, self.config_file)
                
            logger.info("Config saved and validated successfully")
                
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            # Clean up temp file if it exists
            temp_config_file = self.config_file + '.tmp'
            import os
            if os.path.exists(temp_config_file):
                os.remove(temp_config_file)
            raise
    
    def reload_admin_config(self):
        """Reload only admin configuration from config.json to ensure fresh data"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # Reload admin-related data
                self.dev_users = config.get('dev_users', [])
                self.group_admins = defaultdict(list, config.get('group_admins', {}))
                logger.info("Admin configuration reloaded successfully")
        except Exception as e:
            logger.error(f"Error reloading admin config: {e}")
            raise
    
    async def add_course(self, group_id: int, course_id: str, course_name: str, day: int, time: str, bot_instance) -> tuple[bool, str]:
        """Add a new course dynamically to a specific group"""
        # Validate group exists
        if group_id not in self.groups:
            return False, "Group not found! Bot may need to be re-added to the group."
        
        # Validate inputs
        if not course_id or not course_id.strip():
            return False, "Course ID cannot be empty!"
        
        course_id = course_id.strip().lower()
        
        if course_id in self.group_courses.get(group_id, {}):
            return False, f"Course '{course_id}' already exists in this group!"
        
        if not course_name or not course_name.strip():
            return False, "Course name cannot be empty!"
        
        if day < 0 or day > 6:
            return False, "Day must be between 0 (Monday) and 6 (Sunday)!"
        
        # Validate time format
        try:
            time_obj = datetime.strptime(time, "%H:%M").time()
        except ValueError:
            return False, "Time must be in HH:MM format (e.g., '18:00')!"
        
        try:
            # Add to memory
            self.group_courses[group_id][course_id] = course_name.strip()
            self.group_schedules[group_id][course_id] = {"day": day, "time": time}
            self.group_registration_status[group_id][course_id] = False  # Start closed
            self.group_queues[group_id][course_id] = []  # Initialize empty queue
            
            # Save to config file
            self.save_config()
            self.save_data()
            
            # Add scheduler job
            from apscheduler.triggers.cron import CronTrigger
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            
            job_id = f"registration_opener_{group_id}_{course_id}"
            trigger = CronTrigger(
                day_of_week=day,
                hour=int(time.split(':')[0]),
                minute=int(time.split(':')[1]),
                timezone=TIMEZONE
            )
            
            if hasattr(bot_instance, 'application') and bot_instance.application:
                bot_instance.application.job_queue.scheduler.add_job(
                    bot_instance.scheduled_course_registration_opener,
                    trigger=trigger,
                    args=[group_id, course_id],
                    id=job_id,
                    name=f"Registration opener for {course_name} in group {group_id}",
                    replace_existing=True
                )
                
                next_run = trigger.get_next_fire_time(None, datetime.now(TIMEZONE))
                logger.info(f"Scheduled {course_name} registration opening for group {group_id}: {day_names[day]}s at {time} (next: {next_run})")
            
            group_name = self.groups[group_id]['name']
            return True, f"Successfully added course '{course_name}' (ID: {course_id}) to {group_name} with schedule: {day_names[day]} at {time}"
            
        except Exception as e:
            # Rollback changes on error
            self.group_courses[group_id].pop(course_id, None)
            self.group_schedules[group_id].pop(course_id, None)  
            self.group_registration_status[group_id].pop(course_id, None)
            self.group_queues[group_id].pop(course_id, None)
            logger.error(f"Error adding course to group {group_id}: {e}")
            return False, f"Failed to add course: {str(e)}"
    
    async def remove_course(self, group_id: int, course_id: str, bot_instance) -> tuple[bool, str]:
        """Remove a course dynamically from a specific group"""
        # Validate group exists
        if group_id not in self.groups:
            return False, "Group not found! Bot may need to be re-added to the group."
        
        if course_id not in self.group_courses.get(group_id, {}):
            return False, f"Course '{course_id}' does not exist in this group!"
        
        course_name = self.group_courses[group_id][course_id]
        queue_size = len(self.group_queues[group_id].get(course_id, []))
        
        if queue_size > 0:
            return False, f"Cannot remove course '{course_name}' - it has {queue_size} registered students. Clear the queue first."
        
        try:
            # Remove from memory
            removed_name = self.group_courses[group_id].pop(course_id, None)
            self.group_schedules[group_id].pop(course_id, None)
            self.group_registration_status[group_id].pop(course_id, None)
            self.group_queues[group_id].pop(course_id, None)
            
            # Save changes
            self.save_config()
            self.save_data()
            
            # Remove scheduler job
            job_id = f"registration_opener_{group_id}_{course_id}"
            if hasattr(bot_instance, 'application') and bot_instance.application:
                try:
                    bot_instance.application.job_queue.scheduler.remove_job(job_id)
                    logger.info(f"Removed scheduler job for {removed_name} in group {group_id}")
                except Exception as job_error:
                    logger.warning(f"Could not remove scheduler job {job_id}: {job_error}")
            
            group_name = self.groups[group_id]['name']
            return True, f"Successfully removed course '{removed_name}' (ID: {course_id}) from {group_name}"
            
        except Exception as e:
            logger.error(f"Error removing course from group {group_id}: {e}")
            return False, f"Failed to remove course: {str(e)}"
    
    async def check_bot_in_group(self, bot_instance, group_id: int) -> bool:
        """Check if bot is still an active member of the specified group"""
        try:
            # Try to get bot's member status in the group - this is the most reliable method
            bot_member = await bot_instance.get_chat_member(group_id, bot_instance.id)
            # Bot is active if status is 'member', 'administrator', or 'creator'
            # Bot is NOT active if status is 'left' or 'kicked'
            active_statuses = ['member', 'administrator', 'creator']
            is_active = bot_member.status in active_statuses
            logger.info(f"Bot membership check for group {group_id}: status='{bot_member.status}', active={is_active}")
            
            # Additional debugging for disbanded groups
            if bot_member.status not in active_statuses:
                logger.warning(f"Bot has inactive status '{bot_member.status}' in group {group_id}")
                
            return is_active
        except Exception as e:
            # If we can't get member status, the bot is likely not in the group
            logger.warning(f"Bot membership check failed for group {group_id}: {e}")
            logger.error(f"Exception type: {type(e).__name__}, Exception details: {str(e)}")
            # For disbanded groups, this will typically throw a "Chat not found" or "Bad Request" error
            return False
    
    def remove_stale_group(self, group_id: int) -> bool:
        """Remove a group and all its data (courses, queues, schedules, admins)"""
        try:
            group_id_str = str(group_id)
            removed_data = {}
            
            # Remove from groups
            if group_id in self.groups:
                removed_data['group'] = self.groups.pop(group_id)
            
            # Remove courses
            if group_id in self.group_courses:
                removed_data['courses'] = self.group_courses.pop(group_id)
            
            # Remove queues
            if group_id in self.group_queues:
                removed_data['queues'] = self.group_queues.pop(group_id)
            
            # Remove schedules
            if group_id in self.group_schedules:
                removed_data['schedules'] = self.group_schedules.pop(group_id)
            
            # Remove group admins
            if group_id_str in self.group_admins:
                removed_data['admins'] = self.group_admins.pop(group_id_str)
            
            if removed_data:
                logger.info(f"Removed stale group {group_id} and all its data: {list(removed_data.keys())}")
                self.save_config()  # Save to config.json instead of data.json
                return True
            else:
                logger.info(f"No data found for group {group_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error removing stale group {group_id}: {e}")
            return False

# Initialize queue manager
queue_manager = QueueManager()

class UniversityRegistrationBot:
    def __init__(self):
        self.application = None
        # State tracking for multi-step conversations
        self.user_states = {}  # user_id -> conversation state
        
    def set_user_state(self, user_id: int, state: str, data: dict = None):
        """Set conversation state for a user"""
        self.user_states[user_id] = {
            'state': state,
            'data': data or {},
            'timestamp': datetime.now()
        }
    
    def get_user_state(self, user_id: int) -> dict:
        """Get conversation state for a user"""
        return self.user_states.get(user_id, {'state': 'none', 'data': {}})
    
    def clear_user_state(self, user_id: int):
        """Clear conversation state for a user"""
        self.user_states.pop(user_id, None)
    
    async def get_user_display_name(self, user_id: int) -> str:
        """Get user display name (username or full name or user ID)"""
        try:
            # Try to get user info from Telegram
            chat_member = await self.application.bot.get_chat(user_id)
            if chat_member.username:
                return f"@{chat_member.username}"
            elif chat_member.first_name:
                full_name = chat_member.first_name
                if chat_member.last_name:
                    full_name += f" {chat_member.last_name}"
                return full_name
            else:
                return f"User {user_id}"
        except Exception:
            # If we can't get user info, fall back to user ID
            return f"User {user_id}"
    
    def get_chat_context(self, update: Update) -> tuple[int | None, str, bool]:
        """Get chat context: group_id, context_type, is_private_message"""
        chat = update.effective_chat
        is_private = chat.type == 'private'
        
        if is_private:
            # For private messages, get user's associated group
            user_id = update.effective_user.id
            associated_group = queue_manager.get_user_group(user_id)
            return associated_group, 'private', True
        else:
            # For group messages, use the group ID
            return chat.id, 'group', False
    
    async def ensure_group_initialization(self, group_id: int, group_title: str = None):
        """Ensure a group is initialized in the system"""
        if group_id and group_id not in queue_manager.groups:
            queue_manager.initialize_group(group_id, group_title)
            logger.info(f"Auto-initialized group {group_id} ({group_title})")
    
    async def associate_user_with_group(self, user_id: int, group_id: int):
        """Associate user with a group for private message context"""
        if group_id:
            queue_manager.associate_user_with_group(user_id, group_id)
    
    async def handle_new_chat_members(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle when bot is added to a group or new members join"""
        if not update.message or not update.message.new_chat_members:
            return
        
        # Check if bot was added to group
        bot_user = context.bot
        for new_member in update.message.new_chat_members:
            if new_member.id == bot_user.id:
                # Bot was added to this group
                group_id = update.effective_chat.id
                group_title = update.effective_chat.title or f"Group {group_id}"
                
                logger.info(f"Bot added to group {group_id} ({group_title})")
                
                # Initialize the group
                await self.ensure_group_initialization(group_id, group_title)
                
                # Send welcome message to group
                welcome_message = (
                    f"👋 Привет! Я бот для записи на университетские курсы.\n\n"
                    f"📝 **Как начать:**\n"
                    f"1. Отправьте мне /start в личные сообщения\n"
                    f"2. Выберите вашу группу из списка\n"
                    f"3. Используйте /register в личных сообщениях для записи на курсы\n\n"
                    f"🔒 **Важно:** Регистрация происходит только в личных сообщениях!\n\n"
                    f"Используйте /list для просмотра доступных курсов."
                )
                
                try:
                    await update.message.reply_text(welcome_message)
                except Exception as e:
                    logger.error(f"Failed to send welcome message to group {group_id}: {e}")
                
                break
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        group_id, context_type, is_private = self.get_chat_context(update)
        
        # Set up personalized commands for this user
        await self.setup_user_commands(user_id)
        
        # Handle group interactions - DISABLED automatic association to prevent spam and multi-group issues
        if not is_private:
            # group_title = update.effective_chat.title
            # await self.ensure_group_initialization(group_id, group_title)
            # await self.associate_user_with_group(user_id, group_id)
            
            # Direct users to use private messages instead
            await update.message.reply_text(
                f"👋 Привет, {update.effective_user.first_name}!\n"
                f"📱 Отправьте мне /start в личные сообщения для выбора группы и записи на курсы!"
            )
            return
        
        # Private message - check if user has existing group association
        current_group_id = queue_manager.get_user_group(user_id)
        
        if not current_group_id:
            # User has no group - show initial group selection menu
            await self.show_group_selection_menu(update)
            return
        
        # User has a group - show current group status with options
        await self.show_current_group_menu(update, current_group_id)
    
    async def show_group_selection_menu(self, update: Update):
        """Show available groups for user to choose from"""
        available_groups = queue_manager.groups
        
        if not available_groups:
            await update.message.reply_text(
                "❌ На данный момент нет доступных групп.\n"
                "Обратитесь к администратору для добавления групп."
            )
            return
        
        if len(available_groups) == 1:
            # Only one group available, auto-associate
            group_id = list(available_groups.keys())[0]
            user_id = update.effective_user.id
            await self.associate_user_with_group(user_id, group_id)
            
            group_info = available_groups[group_id]
            group_name = group_info.get('name', f'Group {group_id}')
            
            await update.message.reply_text(
                f"✅ Вы автоматически подключены к группе: **{group_name}**\n\n"
                f"Теперь используйте /list для просмотра курсов или /register для записи!",
                parse_mode='Markdown'
            )
            return
        
        # Multiple groups - show selection menu
        keyboard = []
        for group_id, group_info in available_groups.items():
            group_name = group_info.get('name', f'Group {group_id}')
            course_count = len(queue_manager.get_group_courses(int(group_id)))
            button_text = f"📚 {group_name} ({course_count} курс{'ов' if course_count != 1 else ''})"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_group_{group_id}")])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            "📍 **Выберите вашу группу курса:**\n\n"
            "Выберите группу, к которой вы хотите подключиться для записи на курсы.\n"
            "💡 *После выбора группы вы сможете записаться на курсы в этой группе.*"
        )
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def show_current_group_menu(self, update: Update, current_group_id: int):
        """Show current group status with management options"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        user_id = update.effective_user.id
        
        # Get current group info
        group_info = queue_manager.groups.get(current_group_id, {})
        group_name = group_info.get('name', f'Group {current_group_id}')
        
        # Get course count for this group
        group_courses = queue_manager.get_group_courses(current_group_id)
        course_count = len(group_courses)
        
        # Check if user has admin permissions for this group
        is_admin = queue_manager.has_admin_access(user_id, current_group_id)
        admin_status = "👑 Администратор" if is_admin else "👤 Участник"
        
        # Create menu buttons
        keyboard = [
            [InlineKeyboardButton("📋 Просмотреть курсы", callback_data=f"view_courses_{current_group_id}")],
            [InlineKeyboardButton("📝 Записаться на курсы", callback_data=f"register_courses_{current_group_id}")],
            [InlineKeyboardButton("🔄 Сменить группу", callback_data="switch_group")],
            [InlineKeyboardButton("❓ Помощь", callback_data=f"help_{current_group_id}")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            f"👤 **Ваша текущая группа:**\n\n"
            f"📚 **Группа:** {group_name}\n"
            f"📊 **Курсов доступно:** {course_count}\n"
            f"🔰 **Статус:** {admin_status}\n\n"
            f"**Выберите действие:**"
        )
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command with personalized help text"""
        group_id, context_type, is_private = self.get_chat_context(update)
        user_id = update.effective_user.id
        
        if not is_private:
            # Brief help message in group
            await update.message.reply_text(
                "📱 Отправьте мне /help в личные сообщения для полной справки!"
            )
            return
        
        if not group_id:
            # Show group selection menu if no group is associated
            await self.show_group_selection_menu(update)
            return
        
        # Show personalized help text in private message
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        help_text = self.get_user_help_text(user_id, group_name)
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command - show all courses with their schedules and status"""
        group_id, context_type, is_private = self.get_chat_context(update)
        
        if not group_id:
            if is_private:
                # In private message, offer group selection
                await self.show_group_selection_menu(update)
            else:
                # In group chat, show error
                await update.message.reply_text(
                    "❌ Группа не найдена! Сначала взаимодействуйте с ботом в группе курса."
                )
            return
        
        group_courses = queue_manager.get_group_courses(group_id)
        if not group_courses:
            await update.message.reply_text("📚 Нет доступных курсов в этой группе.")
            return
        
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        message_text = f"📚 **Доступные Курсы - {group_name}**\n\n"
        
        days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        
        group_schedules = queue_manager.get_group_schedules(group_id)
        
        for course_id, course_name in group_courses.items():
            # Get schedule info
            schedule_info = group_schedules.get(course_id, {"day": 2, "time": "20:00"})
            day_name = days[schedule_info['day']]
            time_str = schedule_info['time']
            schedule_text = f"{day_name} в {time_str}"
            
            # Get registration status
            is_open = queue_manager.is_course_registration_open(group_id, course_id)
            status_icon = "🟢" if is_open else "🔴"
            status_text = "Открыто" if is_open else "Закрыто"
            
            # Get queue count
            queue_count = len(queue_manager.group_queues[group_id].get(course_id, []))
            
            message_text += f"{status_icon} **{course_name}**\n"
            message_text += f"   • Расписание: {schedule_text}\n"
            message_text += f"   • Статус: {status_text}\n"
            message_text += f"   • Зарегистрировано: {queue_count} студент{'ов' if queue_count != 1 else ''}\n\n"
        
        if is_private:
            message_text += "📱 Используйте /register чтобы записаться на открытый курс!"
        else:
            message_text += "📱 Отправьте /register в личные сообщения для записи на курс!"
        
        await update.message.reply_text(message_text, parse_mode='Markdown')
    
    async def register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /register command - show course menu (private messages only)"""
        group_id, context_type, is_private = self.get_chat_context(update)
        
        # Enforce private message only for registration
        if not is_private:
            await update.message.reply_text(
                "🔒 Регистрация доступна только в личных сообщениях!\n"
                "📱 Отправьте мне /register в личные сообщения."
            )
            return
        
        # Check if user has associated group
        if not group_id:
            await update.message.reply_text(
                "❌ Вы не связаны ни с одной группой!\n\n"
                "Выберите группу из доступных:"
            )
            await self.show_group_selection_menu(update)
            return
        
        # Check if ANY course has registration open for this group
        group_courses = queue_manager.get_group_courses(group_id)
        if not group_courses:
            await update.message.reply_text("📚 Нет доступных курсов в вашей группе.")
            return
        
        open_courses = {course_id: course_name for course_id, course_name in group_courses.items() 
                       if queue_manager.is_course_registration_open(group_id, course_id)}
        
        if not open_courses:
            next_open = self.get_next_registration_time(group_id)
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await update.message.reply_text(
                f"🔒 Регистрация сейчас закрыта для всех курсов в {group_name}.\n"
                f"Следующее открытие: {next_open}",
                parse_mode='Markdown'
            )
            return
        
        keyboard = []
        # Create inline keyboard with ONLY open courses
        for course_id, course_name in open_courses.items():
            count = len(queue_manager.group_queues[group_id].get(course_id, []))
            status = queue_manager.get_course_registration_status(group_id, course_id)
            button_text = f"{course_name} ({count} записано) {status}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"register_{course_id}")])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        message_text = f"📚 **Выберите курс для записи - {group_name}:**\n\n"
        if len(open_courses) < len(group_courses):
            closed_count = len(group_courses) - len(open_courses)
            if closed_count == 1:
                message_text += f"ℹ️ *1 курс сейчас закрыт*\n"
            elif 2 <= closed_count <= 4:
                message_text += f"ℹ️ *{closed_count} курса сейчас закрыты*\n"
            else:  # 5+
                message_text += f"ℹ️ *{closed_count} курсов сейчас закрыты*\n"
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def unregister_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /unregister command - show course menu for unregistration (private messages only)"""
        group_id, context_type, is_private = self.get_chat_context(update)
        
        # Enforce private message only
        if not is_private:
            await update.message.reply_text(
                "🔒 Удаление записи доступно только в личных сообщениях!\n"
                "📱 Отправьте мне /unregister в личные сообщения."
            )
            return
        
        if not group_id:
            await update.message.reply_text(
                "❌ Вы не связаны ни с одной группой!\n\n"
                "Выберите группу из доступных:"
            )
            await self.show_group_selection_menu(update)
            return
        
        user_id = update.effective_user.id
        group_courses = queue_manager.get_group_courses(group_id)
        
        # Check if user has any registrations in this group
        has_registrations = False
        for course_id in group_courses:
            user_entries = [entry for entry in queue_manager.group_queues[group_id][course_id] if entry['user_id'] == user_id]
            if user_entries:
                has_registrations = True
                break
        
        if not has_registrations:
            await update.message.reply_text(
                "❌ Вы не записаны ни на один курс в этой группе.",
                parse_mode='Markdown'
            )
            return
        
        keyboard = []
        # Create inline keyboard with courses where user is registered
        for course_id, course_name in group_courses.items():
            user_entries = [entry for entry in queue_manager.group_queues[group_id][course_id] if entry['user_id'] == user_id]
            if user_entries:
                count = len(user_entries)
                button_text = f"{course_name} ({count} запис{'и' if count > 1 else 'ь'})"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"unregister_{course_id}")])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        await update.message.reply_text(
            f"🗑️ **Выберите курс для удаления записи - {group_name}:**",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command - show course selection menu"""
        group_id, context_type, is_private = self.get_chat_context(update)
        
        if not group_id:
            if is_private:
                # In private message, offer group selection
                await self.show_group_selection_menu(update)
            else:
                # In group chat, show error
                await update.message.reply_text(
                    "❌ Группа не найдена! Сначала взаимодействуйте с ботом в группе курса."
                )
            return
        
        group_courses = queue_manager.get_group_courses(group_id)
        if not group_courses:
            await update.message.reply_text("📚 Нет доступных курсов в этой группе.")
            return
        
        keyboard = []
        # Create inline keyboard with courses showing individual status
        for course_id, course_name in group_courses.items():
            count = len(queue_manager.group_queues[group_id].get(course_id, []))
            status = queue_manager.get_course_registration_status(group_id, course_id)
            button_text = f"{course_name} ({count}) {status}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"status_{course_id}")])
        
        keyboard.append([InlineKeyboardButton("📊 Сводка по всем курсам", callback_data="status_all")])
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Show overall status summary
        open_courses = sum(1 for course_id in group_courses if queue_manager.is_course_registration_open(group_id, course_id))
        total_courses = len(group_courses)
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        status_summary = f"📊 **Статус регистрации - {group_name}:** {open_courses}/{total_courses} курсов открыто"
        
        message = f"{status_summary}\n\n📋 **Выберите курс для просмотра очереди:**"
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def my_registrations_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's registrations (private messages only)"""
        group_id, context_type, is_private = self.get_chat_context(update)
        
        # Enforce private message only
        if not is_private:
            await update.message.reply_text(
                "🔒 Просмотр ваших записей доступен только в личных сообщениях!\n"
                "📱 Отправьте мне /myregistrations в личные сообщения."
            )
            return
        
        if not group_id:
            await update.message.reply_text(
                "❌ Вы не связаны ни с одной группой! Сначала взаимодействуйте с ботом в группе курса."
            )
            return
        
        user_id = update.effective_user.id
        user_registrations = []
        group_courses = queue_manager.get_group_courses(group_id)
        
        for course_id, course_name in group_courses.items():
            queue = queue_manager.group_queues[group_id][course_id]
            for entry in queue:
                if entry['user_id'] == user_id:
                    reg_time = datetime.fromisoformat(entry['registered_at']).strftime("%d.%m %H:%M:%S")
                    user_registrations.append(
                        f"📚 **{course_name}**: {entry['full_name']} (поз. {entry['position']}) - {reg_time}"
                    )
        
        if not user_registrations:
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await update.message.reply_text(f"Вы еще никого не записали в {group_name}.")
            return
        
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        message = f"📝 **Ваши записи - {group_name}:**\n\n" + "\n".join(user_registrations)
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def show_course_detailed_status(self, query, group_id: int, course_id: str):
        """Show detailed status for a specific course in a specific group"""
        group_courses = queue_manager.get_group_courses(group_id)
        if course_id not in group_courses:
            await query.edit_message_text("❌ Недопустимый курс для этой группы.")
            return
        
        course_name = group_courses[course_id]
        queue = queue_manager.group_queues[group_id][course_id]
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        if not queue:
            message = f"📚 **{course_name}** ({group_name})\n\n🔍 Пока нет записей."
        else:
            message = f"📚 **{course_name}** \\({len(queue)} записано\\) - {group_name}\n\n"
            message += "👥 **Записанные студенты:**\n"
            
            for i, entry in enumerate(queue, 1):
                reg_time = datetime.fromisoformat(entry['registered_at']).strftime("%d.%m %H:%M:%S")
                registered_by = entry['username'] if entry['username'] != "Unknown" else f"User {entry['user_id']}"
                message += f"{i}\\. **{entry['full_name']}** \\(от @{registered_by}\\) - {reg_time}\n"
            
            # Add queue limit info
            max_size = queue_manager.get_group_queue_size(group_id)
            remaining = max_size - len(queue)
            if remaining > 0:
                message += f"\n📊 **Статус очереди:** {len(queue)}/{max_size} \\(осталось {remaining} мест\\)"
            else:
                message += f"\n🔴 **Очередь заполнена:** {len(queue)}/{max_size}"
        
        # Add back button
        keyboard = [[InlineKeyboardButton("⬅️ Вернуться к списку курсов", callback_data="back_to_status")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        except Exception as e:
            # Handle markdown parsing errors by falling back to plain text
            if "can't parse entities" in str(e).lower():
                logger.warning(f"Markdown parse error in show_course_detailed_status, falling back to HTML")
                # Remove markdown formatting and use HTML instead
                message_plain = message.replace('**', '').replace('\\(', '(').replace('\\)', ')').replace('\\.', '.')
                try:
                    await query.edit_message_text(message_plain, reply_markup=reply_markup)
                except Exception as e2:
                    logger.error(f"Error in show_course_detailed_status fallback: {e2}")
            else:
                logger.error(f"Error editing message in show_course_detailed_status: {e}")
    
    async def show_all_courses_summary(self, query, group_id: int):
        """Show summary of all courses for a specific group"""
        group_courses = queue_manager.get_group_courses(group_id)
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        if not any(queue_manager.group_queues[group_id].values()):
            message = f"📋 **Сводка курсов - {group_name}**\n\n🔍 Пока нет записей ни на один курс."
        else:
            message = f"📋 **Сводка курсов - {group_name}**\n\n"
            
            for course_id, course_name in group_courses.items():
                count = len(queue_manager.group_queues[group_id][course_id])
                if count > 0:
                    # Show first 3 names for quick overview
                    queue = queue_manager.group_queues[group_id][course_id]
                    first_names = [entry['full_name'] for entry in queue[:3]]
                    names_preview = ", ".join(first_names)
                    if len(queue) > 3:
                        names_preview += f", ... (+{len(queue) - 3} еще)"
                    
                    message += f"📚 **{course_name}**: {count} записано\n"
                    message += f"   👥 {names_preview}\n\n"
                else:
                    message += f"📚 **{course_name}**: 0 записано\n\n"
        
        # Add back button
        keyboard = [[InlineKeyboardButton("⬅️ Вернуться к списку курсов", callback_data="back_to_status")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        except Exception as e:
            # Handle "Message is not modified" and other edit errors silently
            if "not modified" not in str(e).lower():
                logger.error(f"Error editing message in show_all_courses_summary: {e}")
            # For "not modified" errors, we silently ignore since it's expected
    
    async def show_status_selection_menu(self, query, group_id: int):
        """Show the status course selection menu for a specific group"""
        group_courses = queue_manager.get_group_courses(group_id)
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        keyboard = []
        # Create inline keyboard with courses showing individual status
        for course_id, course_name in group_courses.items():
            count = len(queue_manager.group_queues[group_id][course_id])
            status = queue_manager.get_course_registration_status(group_id, course_id)
            button_text = f"{course_name} ({count}) {status}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"status_{course_id}")])
        
        keyboard.append([InlineKeyboardButton("📊 Сводка по всем курсам", callback_data="status_all")])
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Show overall status summary  
        open_courses = sum(1 for course_id in group_courses if queue_manager.is_course_registration_open(group_id, course_id))
        total_courses = len(group_courses)
        status_summary = f"📊 **Статус регистрации - {group_name}:** {open_courses}/{total_courses} курсов открыто"
        
        message = f"{status_summary}\n\n📋 **Выберите курс для просмотра очереди:**"
        
        await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)

    async def show_registration_selection(self, query, group_id: int, course_id: str, user_entries: list):
        """Show selection of specific registrations to remove when user has multiple"""
        group_courses = queue_manager.get_group_courses(group_id)
        course_name = group_courses[course_id]
        
        keyboard = []
        
        for i, entry in enumerate(user_entries):
            reg_time = datetime.fromisoformat(entry['registered_at']).strftime("%d %b, %H:%M")
            button_text = f"📝 {entry['full_name']} - {reg_time}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"remove_{course_id}_{i}")])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = (f"🗑️ **Удалить запись с {course_name}**\n\n"
                  f"У вас {len(user_entries)} запис{'и' if len(user_entries) > 1 else 'ь'}:\n"
                  "Выберите, какую удалить:")
        
        await query.edit_message_text(message, parse_mode='Markdown', reply_markup=reply_markup)

    async def remove_registration(self, query, group_id: int, course_id: str, entry_index: int):
        """Remove specific registration and update data"""
        user_id = query.from_user.id
        user_entries = [entry for entry in queue_manager.group_queues[group_id][course_id] 
                       if entry['user_id'] == user_id]
        
        if entry_index >= len(user_entries):
            await query.edit_message_text("❌ Ошибка: недопустимый выбор записи.")
            return
        
        # Find the actual entry in the full queue
        entry_to_remove = user_entries[entry_index]
        full_queue = queue_manager.group_queues[group_id][course_id]
        
        # Remove the specific entry
        for i, entry in enumerate(full_queue):
            if (entry['user_id'] == entry_to_remove['user_id'] and 
                entry['full_name'] == entry_to_remove['full_name'] and
                entry['registered_at'] == entry_to_remove['registered_at']):
                full_queue.pop(i)
                break
        
        # Update positions for remaining entries
        for i, entry in enumerate(full_queue):
            entry['position'] = i + 1
        
        # Save to file
        queue_manager.save_data()
        
        group_courses = queue_manager.get_group_courses(group_id)
        course_name = group_courses[course_id]
        removed_name = entry_to_remove['full_name']
        
        # Show success message
        remaining_count = len([e for e in queue_manager.group_queues[group_id][course_id] if e['user_id'] == user_id])
        
        message = f"✅ **Успешно удалено!**\n\n"
        message += f"📚 **Курс:** {course_name}\n"
        message += f"👤 **Имя:** {removed_name}\n"
        
        if remaining_count > 0:
            if remaining_count == 1:
                message += f"\n💡 У вас все еще есть 1 другая запись на этот курс."
            else:
                message += f"\n💡 У вас все еще есть {remaining_count} других записи на этот курс."
        
        await query.edit_message_text(message, parse_mode='Markdown')
    
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user = query.from_user
        user_id = query.from_user.id
        
        # Get group context for callback
        group_id, context_type, is_private = self.get_chat_context(update)
        
        if data == "cancel":
            await query.edit_message_text("Операция отменена.")
            return
            
        if data == "cancel_switch":
            user_id = query.from_user.id
            current_group_id = queue_manager.get_user_group(user_id)
            await self.show_current_group_menu_edit(query, current_group_id)
            return

        # Handle dev command callbacks
        if data.startswith("dev_add_admin_group_"):
            group_id = int(data.replace("dev_add_admin_group_", ""))
            if not queue_manager.is_dev(user_id):
                await query.edit_message_text("❌ Access denied. Dev privileges required.")
                return
            
            # Store the group_id for the next step and prompt for user ID
            self.set_user_state(user_id, 'dev_add_admin', {'group_id': group_id})
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            
            await query.edit_message_text(
                f"👑 **Add Admin to {group_name}**\n\n"
                f"Please send the User ID of the person you want to make admin of this group.\n\n"
                f"💡 *Tip: Users can find their ID by messaging @userinfobot*",
                parse_mode='Markdown'
            )
            return

        if data.startswith("dev_remove_admin_group_"):
            group_id = data.replace("dev_remove_admin_group_", "")  # Keep as string
            user_id = query.from_user.id
            if not queue_manager.is_dev(user_id):
                await query.edit_message_text("❌ Access denied. Dev privileges required.")
                return
            
            # Show admin list for this group
            admin_list = queue_manager.group_admins.get(group_id, [])
            if not admin_list:
                await query.edit_message_text("ℹ️ No admins in this group to remove.")
                return
            
            keyboard = []
            # Convert string group_id to int to match groups dictionary  
            try:
                group_id_int = int(group_id)
                group_info = queue_manager.groups.get(group_id_int, {})
                group_name = group_info.get('name', f'Group {group_id}')
            except (ValueError, TypeError):
                group_name = f'Group {group_id}'
            
            for admin_user_id in admin_list:
                admin_name = await self.get_user_display_name(admin_user_id)
                keyboard.append([InlineKeyboardButton(
                    f"Remove {admin_name}", 
                    callback_data=f"dev_confirm_remove_admin_{group_id}_{admin_user_id}"
                )])
            
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"🗑️ **Remove Admin from {group_name}**\n\n"
                f"Select admin to remove:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        if data.startswith("dev_confirm_remove_admin_"):
            parts = data.replace("dev_confirm_remove_admin_", "").split("_", 1)
            if len(parts) == 2:
                group_id, admin_user_id = parts[0], int(parts[1])  # group_id stays as string
                user_id = query.from_user.id
                if not queue_manager.is_dev(user_id):
                    await query.edit_message_text("❌ Access denied. Dev privileges required.")
                    return
                
                # Remove admin from group
                if group_id in queue_manager.group_admins and admin_user_id in queue_manager.group_admins[group_id]:
                    queue_manager.group_admins[group_id].remove(admin_user_id)
                    queue_manager.save_config()
                    
                    # Reload admin config to ensure permissions are immediately removed
                    queue_manager.reload_admin_config()
                    
                    # Convert string group_id to int to match groups dictionary
                    try:
                        group_id_int = int(group_id)
                        group_info = queue_manager.groups.get(group_id_int, {})
                        group_name = group_info.get('name', f'Group {group_id}')
                    except (ValueError, TypeError):
                        group_name = f'Group {group_id}'
                        
                    admin_name = await self.get_user_display_name(admin_user_id)
                    
                    # Update command suggestions for the removed admin
                    await self.setup_user_commands(admin_user_id)
                    
                    await query.edit_message_text(
                        f"✅ Removed admin {admin_name} from {group_name}!\n"
                        f"Their command suggestions have been updated."
                    )
                else:
                    await query.edit_message_text("❌ Admin not found in group.")
            return
        
        # Handle dev cleanup callbacks
        if data.startswith("dev_cleanup_group_"):
            group_id = int(data.replace("dev_cleanup_group_", ""))
            user_id = query.from_user.id
            if not queue_manager.is_dev(user_id):
                await query.edit_message_text("❌ Access denied. Dev privileges required.")
                return
            
            group_info = queue_manager.groups.get(str(group_id), {})
            group_name = group_info.get('name', f'Group {group_id}')
            course_count = len(queue_manager.group_courses.get(str(group_id), {}))
            queue_count = sum(len(q) for q in queue_manager.group_queues.get(str(group_id), {}).values())
            
            # Confirm removal
            keyboard = [
                [InlineKeyboardButton("🗑️ Yes, Remove All Data", callback_data=f"dev_confirm_cleanup_{group_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"⚠️ **Confirm Cleanup**\n\n"
                f"**Group:** {group_name}\n"
                f"**Courses:** {course_count}\n"
                f"**Registrations:** {queue_count}\n\n"
                f"This will permanently delete:\n"
                f"• All course data\n"
                f"• All registration queues\n"
                f"• All schedules\n"
                f"• Group admin assignments\n\n"
                f"**This action cannot be undone!**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return
        
        if data == "dev_cleanup_all_stale":
            user_id = query.from_user.id
            if not queue_manager.is_dev(user_id):
                await query.edit_message_text("❌ Access denied. Dev privileges required.")
                return
            
            await query.edit_message_text("🔍 Checking all groups and removing stale ones...")
            
            removed_count = 0
            total_courses = 0
            total_registrations = 0
            
            # Check each group and remove stale ones
            stale_groups = []
            for group_id, group_info in list(queue_manager.groups.items()):
                is_member = await queue_manager.check_bot_in_group(self.application.bot, group_id)
                if not is_member:
                    group_name = group_info.get('name', f'Group {group_id}')
                    course_count = len(queue_manager.group_courses.get(group_id, {}))
                    queue_count = sum(len(q) for q in queue_manager.group_queues.get(group_id, {}).values())
                    
                    stale_groups.append((group_id, group_name, course_count, queue_count))
                    total_courses += course_count
                    total_registrations += queue_count
            
            # Remove all stale groups
            for group_id, group_name, course_count, queue_count in stale_groups:
                if queue_manager.remove_stale_group(group_id):
                    removed_count += 1
            
            if removed_count > 0:
                message = f"✅ **Cleanup Complete!**\n\n"
                message += f"**Removed {removed_count} stale groups:**\n"
                for group_id, group_name, course_count, queue_count in stale_groups:
                    message += f"• {group_name} ({course_count} courses, {queue_count} registrations)\n"
                message += f"\n**Total cleaned:** {total_courses} courses, {total_registrations} registrations"
            else:
                message = "ℹ️ No stale groups found to remove."
            
            await query.edit_message_text(message, parse_mode='Markdown')
            return
        
        if data.startswith("dev_confirm_cleanup_"):
            group_id = int(data.replace("dev_confirm_cleanup_", ""))
            user_id = query.from_user.id
            if not queue_manager.is_dev(user_id):
                await query.edit_message_text("❌ Access denied. Dev privileges required.")
                return
            
            group_info = queue_manager.groups.get(str(group_id), {})
            group_name = group_info.get('name', f'Group {group_id}')
            course_count = len(queue_manager.group_courses.get(str(group_id), {}))
            queue_count = sum(len(q) for q in queue_manager.group_queues.get(str(group_id), {}).values())
            
            if queue_manager.remove_stale_group(group_id):
                await query.edit_message_text(
                    f"✅ **Successfully Removed**\n\n"
                    f"**Group:** {group_name}\n"
                    f"**Removed:** {course_count} courses, {queue_count} registrations\n\n"
                    f"All data has been permanently deleted.",
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text("❌ Failed to remove group data.")
            return
        
        if data.startswith("select_group_"):
            # Handle group selection
            selected_group_id = int(data.replace("select_group_", ""))
            user_id = user.id
            
            # Validate group exists
            if selected_group_id not in queue_manager.groups:
                await query.edit_message_text("❌ Выбранная группа не найдена!")
                return
            
            # Associate user with selected group
            await self.associate_user_with_group(user_id, selected_group_id)
            
            # Show success message
            group_info = queue_manager.groups[selected_group_id]
            group_name = group_info.get('name', f'Group {selected_group_id}')
            course_count = len(queue_manager.get_group_courses(selected_group_id))
            
            success_message = f"""✅ **Успешно подключены к группе!**

📍 **Группа:** {group_name}
📚 **Курсов доступно:** {course_count}

🎯 **Что дальше:**
• `/list` - Просмотр доступных курсов и расписаний
• `/register` - Запись на открытые курсы
• `/status` - Проверка текущих очередей

Добро пожаловать! 🎓"""
            
            await query.edit_message_text(success_message, parse_mode='Markdown')
            return
        
        if data.startswith("register_"):
            course_id = data.replace("register_", "")
            
            # Validate group context
            if not group_id:
                await query.edit_message_text("❌ Группа не найдена! Повторите попытку после взаимодействия с ботом в группе курса.")
                return
            
            # Validate course exists in this group
            group_courses = queue_manager.get_group_courses(group_id)
            if course_id not in group_courses:
                await query.edit_message_text("❌ Недопустимый курс для этой группы!")
                return
            
            # Ask for full name
            course_name = group_courses[course_id]
            await query.edit_message_text(
                f"📝 Вы выбрали: **{course_name}**\n\n"
                "Ответьте с полным именем для записи:\n"
                "💡 *Вы можете записать себя или друзей*\n"
                "⚠️ *Каждое имя может быть записано только один раз на курс*",
                parse_mode='Markdown'
            )
            
            # Store course and group selection in user data
            context.user_data['selected_course'] = course_id
            context.user_data['selected_group'] = group_id
            context.user_data['awaiting_name'] = True
            return
        
        if data.startswith("status_"):
            if not group_id:
                await query.edit_message_text("❌ Группа не найдена!")
                return
                
            if data == "status_all":
                # Show summary of all courses for this group
                await self.show_all_courses_summary(query, group_id)
            else:
                # Show detailed status for specific course
                course_id = data.replace("status_", "")
                await self.show_course_detailed_status(query, group_id, course_id)
            return
        
        if data.startswith("unregister_"):
            course_id = data.replace("unregister_", "")
            user_id = user.id
            
            # Validate group context
            if not group_id:
                await query.edit_message_text("❌ Группа не найдена! Сначала взаимодействуйте с ботом в группе курса.")
                return
            
            # Validate that the course exists in the group
            group_courses = queue_manager.get_group_courses(group_id)
            if not group_courses or course_id not in group_courses:
                await query.edit_message_text("❌ Курс не найден в вашей группе!")
                return
            
            # Get all registrations for this user in this course in this group
            user_entries = [entry for entry in queue_manager.group_queues[group_id][course_id] 
                          if entry['user_id'] == user_id]
            
            if not user_entries:
                group_courses = queue_manager.get_group_courses(group_id)
                course_name = group_courses.get(course_id, course_id)
                await query.edit_message_text(
                    f"❌ Вы не записаны на {course_name}."
                )
                return
            
            if len(user_entries) == 1:
                # Only one registration, remove it directly
                await self.remove_registration(query, group_id, course_id, 0)  # Index 0 for single entry
                return
            else:
                # Multiple registrations, show selection
                await self.show_registration_selection(query, group_id, course_id, user_entries)
                return
        
        # Handle course removal selection (must be before general remove_ handler)
        if data.startswith("remove_course_"):
            parts = data.replace("remove_course_", "").split("_", 1)
            if len(parts) == 2:
                group_id, course_id = int(parts[0]), parts[1]
                await self.handle_remove_course_callback(query, group_id, course_id, context)
            else:
                # Legacy format support (no group_id) - find group from course_id and chat context
                course_id = data.replace("remove_course_", "")
                
                # Get group_id from chat context
                chat_id = query.message.chat_id
                chat_type = query.message.chat.type
                
                if chat_type in [Chat.GROUP, Chat.SUPERGROUP]:
                    target_group_id = chat_id
                else:
                    # For private chats, find the group that contains this course
                    target_group_id = None
                    for gid, group_data in queue_manager.groups.items():
                        if course_id in group_data.get('courses', {}):
                            target_group_id = gid
                            break
                
                if target_group_id:
                    await self.handle_remove_course_callback(query, target_group_id, course_id, context)
                else:
                    await query.edit_message_text("❌ Course not found or access denied!")
                    await query.answer()
            return
        
        if data.startswith("remove_"):
            # Format: remove_{course_id}_{index}
            parts = data.replace("remove_", "").split("_")
            if len(parts) >= 2:
                # Join all parts except the last one (which should be the index)
                course_id = "_".join(parts[:-1])
                try:
                    entry_index = int(parts[-1])
                    if not group_id:
                        await query.edit_message_text("❌ Группа не найдена!")
                        return
                    await self.remove_registration(query, group_id, course_id, entry_index)
                    return
                except ValueError:
                    logger.error(f"Invalid entry index in callback data: {data}")
                    await query.answer("Invalid data format")
                    return
            else:
                logger.error(f"Invalid remove callback format: {data}")
                await query.answer("Invalid data format")
                return

        if data == "back_to_status":
            # Recreate the status selection menu
            if not group_id:
                await query.edit_message_text("❌ Группа не найдена!")
                return
            await self.show_status_selection_menu(query, group_id)
            return

        if data.startswith("admin_swap_group_"):
            group_id = int(data.replace("admin_swap_group_", ""))
            user_id = query.from_user.id
            
            # Check if user still has admin access to this group
            if not queue_manager.has_admin_access(user_id, group_id):
                await query.answer("❌ Доступ запрещён. У вас нет прав администратора в этой группе.", show_alert=True)
                return
                
            # Mock an update object for show_swap_courses_for_group
            class MockUpdate:
                def __init__(self, query):
                    self.callback_query = query
                    self.effective_user = query.from_user
                    self.message = None
            
            mock_update = MockUpdate(query)
            await self.show_swap_courses_for_group(mock_update, group_id)
            return

        if data.startswith("admin_swap_"):
            course_id = data.replace("admin_swap_", "")
            await self.show_swap_interface(query, course_id, context)
            return

        # Handle admin group selection for opening courses
        if data.startswith("admin_open_group_"):
            group_id = int(data.replace("admin_open_group_", ""))
            group_courses = queue_manager.get_group_courses(group_id)
            if not group_courses:
                await query.edit_message_text("❌ No courses found in this group!")
                return
            
            keyboard = []
            for course_id, course_name in group_courses.items():
                is_open = queue_manager.is_course_registration_open(group_id, course_id)
                status = "🟢" if is_open else "🔴"
                keyboard.append([InlineKeyboardButton(
                    f"{course_name} {status}", 
                    callback_data=f"admin_open_course_{group_id}_{course_id}"
                )])
            
            # Add option to open all courses in this group
            keyboard.append([InlineKeyboardButton("🟢 Open ALL in Group", callback_data=f"admin_open_all_{group_id}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(
                f"🟢 **Open Registration - {group_name}**\n\n"
                f"Select a course to open:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        # Handle admin group selection for closing courses
        if data.startswith("admin_close_group_"):
            group_id = int(data.replace("admin_close_group_", ""))
            group_courses = queue_manager.get_group_courses(group_id)
            if not group_courses:
                await query.edit_message_text("❌ No courses found in this group!")
                return
            
            keyboard = []
            for course_id, course_name in group_courses.items():
                is_open = queue_manager.is_course_registration_open(group_id, course_id)
                status = "🟢" if is_open else "🔴"
                keyboard.append([InlineKeyboardButton(
                    f"{course_name} {status}", 
                    callback_data=f"admin_close_course_{group_id}_{course_id}"
                )])
            
            # Add option to close all courses in this group
            keyboard.append([InlineKeyboardButton("🔴 Close ALL in Group", callback_data=f"admin_close_all_{group_id}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(
                f"🔴 **Close Registration - {group_name}**\n\n"
                f"Select a course to close:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        # Handle admin group selection for removing courses
        if data.startswith("admin_remove_group_"):
            group_id = int(data.replace("admin_remove_group_", ""))
            group_courses = queue_manager.get_group_courses(group_id)
            if not group_courses:
                await query.edit_message_text("❌ No courses found in this group!")
                return
            
            keyboard = []
            for course_id, course_name in group_courses.items():
                queue_size = len(queue_manager.group_queues.get(group_id, {}).get(course_id, []))
                status_text = f" ({queue_size} registered)" if queue_size > 0 else ""
                keyboard.append([InlineKeyboardButton(
                    f"🗑️ {course_name}{status_text}",
                    callback_data=f"remove_course_{group_id}_{course_id}"
                )])
            
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(
                f"🗑️ <b>Remove Course - {group_name}</b>\n\n"
                f"⚠️ <b>Warning</b>: This will permanently delete the course and all its data.\n"
                f"Courses with registered students cannot be removed.\n\n"
                f"Select a course to remove:",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return

        # Handle admin group selection for clearing queues
        if data.startswith("admin_clear_group_"):
            logger.info(f"admin_clear_group callback received: {data}")
            group_id = int(data.replace("admin_clear_group_", ""))
            group_courses = queue_manager.get_group_courses(group_id)
            logger.info(f"Found {len(group_courses)} courses in group {group_id}")
            if not group_courses:
                await query.edit_message_text("❌ No courses found in this group!")
                return
            
            # Build keyboard for courses with non-empty queues in this group
            keyboard = []
            total_registered = 0
            has_queues = False
            
            for course_id, course_name in group_courses.items():
                queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                if queue:
                    has_queues = True
                    queue_size = len(queue)
                    total_registered += queue_size
                    keyboard.append([InlineKeyboardButton(
                        f"🗑️ Clear {course_name} ({queue_size} registered)", 
                        callback_data=f"admin_clear_{group_id}_{course_id}"
                    )])
            
            if not has_queues:
                await query.edit_message_text("📭 All queues in this group are already empty!")
                return
            
            # Add option to clear all queues in this group
            keyboard.append([InlineKeyboardButton(f"🗑️ Clear All Queues ({total_registered} total)", callback_data=f"admin_clear_all_confirm_{group_id}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(
                f"🗑️ **Clear Queues - {group_name}**\n\n"
                f"Select a queue to clear:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return

        # Handle admin group selection for status viewing
        if data.startswith("admin_status_group_"):
            try:
                group_id = int(data.replace("admin_status_group_", ""))
                logger.info(f"Admin status callback for group_id: {group_id} (type: {type(group_id)})")
                
                group_courses = queue_manager.get_group_courses(group_id)
                logger.info(f"Found {len(group_courses)} courses for group {group_id}")
                
                if not group_courses:
                    await query.edit_message_text("❌ No courses found in this group!")
                    return
                
                # Build detailed status for this group
                group_info = queue_manager.groups.get(group_id, {})
                group_name = group_info.get('name', f'Group {group_id}')
                status_text = f"📊 <b>Detailed Queue Status - {group_name}</b>\n\n"
                
                total_registered = 0
                for course_id, course_name in group_courses.items():
                    queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                    total_registered += len(queue)
                    logger.info(f"Course {course_id}: {len(queue)} registrations")
                    status_text += f"📚 <b>{course_name}</b> ({len(queue)} registered):\n"
                    
                    if queue:
                        for i, entry in enumerate(queue, 1):
                            reg_time = datetime.fromisoformat(entry['registered_at']).strftime("%H:%M:%S")
                            logger.info(f"Processing entry {i}: full_name='{entry['full_name']}', username='{entry['username']}'")
                            # Escape HTML characters in user data
                            full_name = entry['full_name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            username = entry['username'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                            status_text += f"  {i}. {full_name} (@{username}) - {reg_time}\n"
                    else:
                        status_text += "  No registrations\n"
                    status_text += "\n"
                
                status_text += f"<b>Total registrations in group: {total_registered}</b>"
                
                await query.edit_message_text(status_text, parse_mode='HTML')
                return
                
            except Exception as e:
                logger.error(f"Error in admin_status_group callback: {e}", exc_info=True)
                await query.edit_message_text("❌ Sorry, an error occurred. Please try again later.")
                return

        # Handle admin group selection for adding courses
        if data.startswith("admin_add_course_group_"):
            group_id = int(data.replace("admin_add_course_group_", ""))
            
            # Check if user has admin access to this group
            if not queue_manager.has_admin_access(user_id, group_id):
                await query.edit_message_text("❌ Доступ запрещён. У вас нет прав администратора для этой группы.")
                return
            
            # Start the add course conversation with group context
            self.set_user_state(user_id, 'add_course_id', {'group_id': group_id})
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            
            await query.edit_message_text(
                f"➕ **Добавить новый курс**\n\n"
                f"Добавление курса в: **{group_name}**\n\n"
                "Шаг 1/4: Введите ID курса (короткий идентификатор, строчными буквами, например, 'math101', 'phys201'):\n\n"
                "Введите ID курса или отправьте /cancel для отмены.",
                parse_mode='Markdown'
            )
            return

        # Handle opening specific courses
        if data.startswith("admin_open_course_"):
            parts = data.replace("admin_open_course_", "").split("_", 1)
            if len(parts) == 2:
                group_id, course_id = int(parts[0]), parts[1]
                group_courses = queue_manager.get_group_courses(group_id)
                if course_id in group_courses:
                    queue_manager.set_course_registration_status(group_id, course_id, True)
                    course_name = group_courses[course_id]
                    group_info = queue_manager.groups.get(group_id, {})
                    group_name = group_info.get('name', f'Group {group_id}')
                    await query.edit_message_text(f"✅ Registration opened for {course_name} in {group_name}!")
                else:
                    await query.edit_message_text("❌ Course not found!")
            return

        # Handle closing specific courses
        if data.startswith("admin_close_course_"):
            parts = data.replace("admin_close_course_", "").split("_", 1)
            if len(parts) == 2:
                group_id, course_id = int(parts[0]), parts[1]
                group_courses = queue_manager.get_group_courses(group_id)
                if course_id in group_courses:
                    queue_manager.set_course_registration_status(group_id, course_id, False)
                    course_name = group_courses[course_id]
                    group_info = queue_manager.groups.get(group_id, {})
                    group_name = group_info.get('name', f'Group {group_id}')
                    await query.edit_message_text(f"🔒 Registration closed for {course_name} in {group_name}!")
                else:
                    await query.edit_message_text("❌ Course not found!")
            return

        # Handle opening all courses in a group
        if data.startswith("admin_open_all_"):
            group_id = int(data.replace("admin_open_all_", ""))
            group_courses = queue_manager.get_group_courses(group_id)
            count = 0
            for course_id in group_courses:
                queue_manager.set_course_registration_status(group_id, course_id, True)
                count += 1
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(f"✅ Registration opened for ALL {count} courses in {group_name}!")
            return

        # Handle closing all courses in a group  
        if data.startswith("admin_close_all_"):
            group_id = int(data.replace("admin_close_all_", ""))
            group_courses = queue_manager.get_group_courses(group_id)
            count = 0
            for course_id in group_courses:
                queue_manager.set_course_registration_status(group_id, course_id, False)
                count += 1
            
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(f"🔒 Registration closed for ALL {count} courses in {group_name}!")
            return

        if data.startswith("admin_open_"):
            if data == "admin_open_all":
                # Open all courses
                queue_manager.open_registration()
                await query.edit_message_text("✅ Registration opened for ALL courses!")
                await self.notify_registration_open(context)
            else:
                # Open specific course
                course_id = data.replace("admin_open_", "")
                if course_id in queue_manager.courses:
                    queue_manager.open_course_registration(course_id)
                    course_name = queue_manager.courses[course_id]
                    await query.edit_message_text(f"✅ Registration opened for {course_name}!")
            return

        if data.startswith("admin_close_"):
            if data == "admin_close_all":
                # Close all courses
                queue_manager.close_registration()
                await query.edit_message_text("🔒 Registration closed for ALL courses!")
            else:
                # Close specific course
                course_id = data.replace("admin_close_", "")
                if course_id in queue_manager.courses:
                    queue_manager.close_course_registration(course_id)
                    course_name = queue_manager.courses[course_id]
                    await query.edit_message_text(f"🔒 Registration closed for {course_name}!")
            return
        
        # Handle add course day selection
        if data.startswith("add_day_"):
            day = int(data.replace("add_day_", ""))
            await self.handle_add_course_day_callback(query, day, context)
            return
        
        # Handle course removal confirmation
        if data.startswith("confirm_remove_"):
            parts = data.replace("confirm_remove_", "").split("_", 1)
            if len(parts) == 2:
                group_id, course_id = int(parts[0]), parts[1]
                await self.handle_confirm_remove_callback(query, group_id, course_id, context)
            else:
                # Legacy format support (no group_id) - find group from course_id and chat context
                course_id = data.replace("confirm_remove_", "")
                
                # Get group_id from chat context
                chat_id = query.message.chat_id
                chat_type = query.message.chat.type
                
                if chat_type in [Chat.GROUP, Chat.SUPERGROUP]:
                    target_group_id = chat_id
                else:
                    # For private chats, find the group that contains this course
                    target_group_id = None
                    for gid, group_data in queue_manager.groups.items():
                        if course_id in group_data.get('courses', {}):
                            target_group_id = gid
                            break
                
                if target_group_id:
                    await self.handle_confirm_remove_callback(query, target_group_id, course_id, context)
                else:
                    await query.edit_message_text("❌ Course not found or access denied!")
                    await query.answer()
            return
        
        # Handle admin clear callbacks
        if data.startswith("admin_clear_"):
            if data.startswith("admin_clear_all_confirm_"):
                # Clear all queues in specific group
                group_id = int(data.replace("admin_clear_all_confirm_", ""))
                group_courses = queue_manager.get_group_courses(group_id)
                total_cleared = 0
                
                for course_id in group_courses:
                    queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                    total_cleared += len(queue)
                    queue_manager.clear_course_queue(group_id, course_id)
                
                group_info = queue_manager.groups.get(group_id, {})
                group_name = group_info.get('name', f'Group {group_id}')
                await query.edit_message_text(f"🗑️ All queues cleared in {group_name}! ({total_cleared} registrations removed)")
            elif data == "dev_clearQ_all_confirm":
                # Global clear: Clear all queues in all groups
                user_id = query.from_user.id
                if not queue_manager.is_dev(user_id):
                    await query.edit_message_text("❌ Access denied. Dev privileges required.")
                    return
                
                total_cleared = 0
                groups_cleared = 0
                cleared_details = []
                
                for group_id, group_info in queue_manager.groups.items():
                    group_name = group_info.get('name', f'Group {group_id}')
                    group_courses = queue_manager.get_group_courses(group_id)
                    group_cleared = 0
                    
                    for course_id in group_courses:
                        queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                        group_cleared += len(queue)
                    
                    if group_cleared > 0:
                        queue_manager.clear_queues(group_id)
                        total_cleared += group_cleared
                        groups_cleared += 1
                        cleared_details.append(f"• {group_name}: {group_cleared} cleared")
                
                if total_cleared > 0:
                    details = "\n".join(cleared_details[:5])
                    if len(cleared_details) > 5:
                        details += f"\n... and {len(cleared_details) - 5} more groups"
                    
                    message = (
                        f"✅ **Global Clear Complete!**\n\n"
                        f"**Results:**\n"
                        f"• **{total_cleared} total registrations** removed\n"
                        f"• **{groups_cleared} groups** cleared\n\n"
                        f"**Details:**\n{details}\n\n"
                        f"All affected students will need to re-register."
                    )
                    await query.edit_message_text(message, parse_mode='Markdown')
                else:
                    await query.edit_message_text("ℹ️ All queues were already empty - nothing was cleared!")
                
                await query.answer("Global clear completed!")
                return
            else:
                # Clear specific course - new format: admin_clear_group_id_course_id
                parts = data.replace("admin_clear_", "").split("_", 1)
                if len(parts) == 2:
                    group_id, course_id = int(parts[0]), parts[1]
                    group_courses = queue_manager.get_group_courses(group_id)
                    if course_id in group_courses:
                        course_name = group_courses[course_id]
                        queue_count = len(queue_manager.group_queues.get(group_id, {}).get(course_id, []))
                        queue_manager.clear_course_queue(group_id, course_id)
                        group_info = queue_manager.groups.get(group_id, {})
                        group_name = group_info.get('name', f'Group {group_id}')
                        await query.edit_message_text(f"🗑️ Queue cleared for {course_name} in {group_name}! ({queue_count} registrations removed)")
                    else:
                        await query.edit_message_text("❌ Course not found in this group!")
                else:
                    # Legacy format support (no group_id) - should not be used in multi-group setup
                    course_id = data.replace("admin_clear_", "")
                    # Find which group this course belongs to
                    target_group_id = None
                    for gid, group_data in queue_manager.groups.items():
                        if course_id in group_data.get('courses', {}):
                            target_group_id = gid
                            break
                    
                    if target_group_id and queue_manager.has_admin_access(user_id, target_group_id):
                        course_name = queue_manager.groups[target_group_id]['courses'][course_id]['name']
                        queue_count = len(queue_manager.group_queues.get(target_group_id, {}).get(course_id, []))
                        queue_manager.clear_course_queue(target_group_id, course_id)
                        await query.edit_message_text(f"🗑️ Queue cleared for {course_name}! ({queue_count} registrations removed)")
                    else:
                        await query.edit_message_text("❌ Course not found or access denied!")
            return
        
        # Handle admin queue size callbacks
        if data.startswith("queuesize_"):
            user_id = query.from_user.id
            if not queue_manager.has_admin_access(user_id):
                await query.edit_message_text("❌ Access denied. Admin privileges required.")
                await query.answer()
                return
            
            try:
                # Parse callback data: queuesize_group_id_new_size
                parts = data.replace("queuesize_", "").split("_")
                if len(parts) >= 2:
                    group_id_str = "_".join(parts[:-1])  # Handle negative group IDs
                    new_size = int(parts[-1])
                    group_id = int(group_id_str)
                    
                    # Verify admin access to this specific group
                    if not queue_manager.has_admin_access(user_id, group_id):
                        await query.edit_message_text("❌ You don't have admin access to this group.")
                        await query.answer()
                        return
                    
                    # Set the queue size
                    if queue_manager.set_group_queue_size(group_id, new_size):
                        group_info = queue_manager.groups.get(group_id, {})
                        group_name = group_info.get('name', f'Group {group_id_str}')
                        await query.edit_message_text(
                            f"✅ **Queue size updated successfully!**\n\n"
                            f"**Group:** {group_name}\n"
                            f"**New queue size:** {new_size}",
                            parse_mode='Markdown'
                        )
                        logger.info(f"Queue size for group {group_id} ({group_name}) set to {new_size} by user {user_id}")
                    else:
                        await query.edit_message_text("❌ Failed to update queue size.")
                else:
                    await query.edit_message_text("❌ Invalid callback data.")
            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing queue size callback data: {data}, error: {e}")
                await query.edit_message_text("❌ Error processing request.")
            
            await query.answer()
            return
        
        # Handle dev queue size callbacks
        if data.startswith("dev_queuesize_"):
            user_id = query.from_user.id
            if not queue_manager.is_dev(user_id):
                await query.edit_message_text("❌ Access denied. Dev privileges required.")
                await query.answer()
                return
            
            try:
                # Parse callback data: dev_queuesize_group_id_new_size
                parts = data.replace("dev_queuesize_", "").split("_")
                if len(parts) >= 2:
                    group_id_str = "_".join(parts[:-1])  # Handle negative group IDs
                    new_size = int(parts[-1])
                    group_id = int(group_id_str)
                    
                    # Set the queue size (dev can modify any group)
                    if queue_manager.set_group_queue_size(group_id, new_size):
                        group_info = queue_manager.groups.get(group_id, {})
                        group_name = group_info.get('name', f'Group {group_id_str}')
                        await query.edit_message_text(
                            f"✅ **Queue size updated successfully!**\n\n"
                            f"**Group:** {group_name}\n"
                            f"**New queue size:** {new_size}\n"
                            f"**Updated by:** Dev user",
                            parse_mode='Markdown'
                        )
                        logger.info(f"Queue size for group {group_id} ({group_name}) set to {new_size} by dev user {user_id}")
                    else:
                        await query.edit_message_text("❌ Failed to update queue size.")
                else:
                    await query.edit_message_text("❌ Invalid callback data.")
            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing dev queue size callback data: {data}, error: {e}")
                await query.edit_message_text("❌ Error processing request.")
            
            await query.answer()
            return
        
        # Handle group management callbacks
        if data == "switch_group":
            await self.handle_switch_group_callback(query, context)
            return
        
        if data.startswith("view_courses_"):
            group_id = int(data.replace("view_courses_", ""))
            await self.handle_view_courses_callback(query, group_id, context)
            return
            
        if data.startswith("register_courses_"):
            group_id = int(data.replace("register_courses_", ""))
            await self.handle_register_courses_callback(query, group_id, context)
            return
            
        if data.startswith("help_"):
            group_id = int(data.replace("help_", ""))
            await self.handle_help_callback(query, group_id, context)
            return
            
        if data.startswith("confirm_switch_"):
            new_group_id = int(data.replace("confirm_switch_", ""))
            await self.handle_confirm_switch_callback(query, new_group_id, context)
            return
            
        if data.startswith("back_to_menu_"):
            group_id = int(data.replace("back_to_menu_", ""))
            await self.handle_back_to_menu_callback(query, group_id, context)
            return
    
    async def message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (for name input, admin swap positions, and add course conversation)"""
        user_id = update.effective_user.id
        group_id, context_type, is_private = self.get_chat_context(update)
        
        # Set up personalized commands for first-time users or when permissions might have changed
        await self.setup_user_commands(user_id)
        
        # Handle cancel command
        if update.message.text.strip().lower() in ['/cancel', 'cancel']:
            if user_id in self.user_states:
                self.clear_user_state(user_id)
                await update.message.reply_text("❌ Операция отменена.")
                return
            context.user_data.clear()
            await update.message.reply_text("❌ Операция отменена.")
            return
        
        # Check for add course conversation
        user_state = self.get_user_state(user_id)
        if user_state.get('state') in ['add_course_id', 'add_course_name', 'add_course_time']:
            await self.handle_add_course_conversation(update, context)
            return
        
        # Check for dev add admin conversation
        if user_state.get('state') == 'dev_add_admin':
            await self.handle_dev_add_admin_conversation(update, context)
            return
        
        # Check if awaiting swap positions (admin functionality)
        if context.user_data.get('awaiting_swap_positions'):
            swap_group_id = context.user_data.get('swap_group_id')
            if not queue_manager.has_admin_access(user_id, swap_group_id):
                await update.message.reply_text("❌ Доступ запрещен. Нужны права администратора.")
                context.user_data.clear()
                return
            
            await self.process_swap_positions(update, context, update.message.text)
            return
        
        # Handle name input for registration
        if not context.user_data.get('awaiting_name'):
            return
        
        # Enforce private message for registration
        if not is_private:
            await update.message.reply_text("🔒 Регистрация доступна только в личных сообщениях!")
            return
        
        user = update.effective_user
        full_name = update.message.text.strip()
        course_id = context.user_data.get('selected_course')
        selected_group_id = context.user_data.get('selected_group')
        
        # Validate we have all required data
        if not course_id or not selected_group_id or len(full_name) < 2:
            await update.message.reply_text("Пожалуйста, укажите правильное полное имя.")
            return
        
        # Validate user has access to this group
        if group_id != selected_group_id:
            await update.message.reply_text("❌ Ошибка: несоответствие группы! Начните регистрацию сначала.")
            context.user_data.clear()
            return
        
        # Register user
        success, message = queue_manager.add_to_queue(
            selected_group_id,
            course_id, 
            user.id, 
            user.username or "Unknown", 
            full_name
        )
        
        # Clear user data
        context.user_data.clear()
        
        if success:
            await update.message.reply_text(f"✅ {message}")
        else:
            await update.message.reply_text(f"❌ {message}")
    
    # Admin commands
    async def admin_open_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Open registration for specific course"""
        user_id = update.effective_user.id
        logger.info(f"admin_open_command called by user {user_id}")
        
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return
        
        # Show group selection first (filter by admin access)
        keyboard = []
        logger.info(f"Checking groups for admin_open. Total groups: {len(queue_manager.groups)}")
        for group_id, group_info in queue_manager.groups.items():
            # Check if user has admin access to this group (convert group_id to int)
            try:
                group_id_int = int(group_id)
            except (ValueError, TypeError):
                logger.warning(f"Could not convert group_id {group_id} to int")
                continue
                
            logger.info(f"Checking access for user {user_id} to group {group_id_int}")
            if queue_manager.has_admin_access(user_id, group_id_int):
                logger.info(f"User {user_id} has admin access to group {group_id_int}")
                group_name = group_info.get('name', f'Group {group_id}')
                group_courses = queue_manager.get_group_courses(group_id_int)
                course_count = len(group_courses)
                logger.info(f"Group {group_id_int} ({group_name}) has {course_count} courses: {list(group_courses.keys())}")
                keyboard.append([InlineKeyboardButton(
                    f"{group_name} ({course_count} courses)", 
                    callback_data=f"admin_open_group_{group_id_int}"
                )])
            else:
                logger.info(f"User {user_id} does NOT have admin access to group {group_id_int}")
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🟢 **Open Course Registration**\n\n"
            "Select a group to manage:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def admin_close_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Close registration for specific course"""
        user_id = update.effective_user.id
        logger.info(f"admin_close_command called by user {user_id}")
        
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return
        
        # Show group selection first (filter by admin access)
        keyboard = []
        logger.info(f"Checking groups for admin_close. Total groups: {len(queue_manager.groups)}")
        for group_id, group_info in queue_manager.groups.items():
            # Check if user has admin access to this group (convert group_id to int)
            try:
                group_id_int = int(group_id)
            except (ValueError, TypeError):
                logger.warning(f"Could not convert group_id {group_id} to int")
                continue
                
            logger.info(f"Checking access for user {user_id} to group {group_id_int}")
            if queue_manager.has_admin_access(user_id, group_id_int):
                logger.info(f"User {user_id} has admin access to group {group_id_int}")
                group_name = group_info.get('name', f'Group {group_id}')
                group_courses = queue_manager.get_group_courses(group_id_int)
                course_count = len(group_courses)
                logger.info(f"Group {group_id_int} ({group_name}) has {course_count} courses: {list(group_courses.keys())}")
                keyboard.append([InlineKeyboardButton(
                    f"{group_name} ({course_count} courses)", 
                    callback_data=f"admin_close_group_{group_id_int}"
                )])
            else:
                logger.info(f"User {user_id} does NOT have admin access to group {group_id_int}")
        
        # Add option to close all courses
        keyboard.append([InlineKeyboardButton("🔴 Close ALL Courses", callback_data="admin_close_all")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "� **Close Course Registration**\n\n"
            "Select a course to close registration:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def admin_clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Clear specific queue"""
        user_id = update.effective_user.id
        logger.info(f"admin_clear_command called by user {user_id}")
        
        if not queue_manager.has_admin_access(user_id):
            logger.warning(f"Access denied for user {user_id} in admin_clear_command")
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return

        # Show group selection first (filter by admin access)
        keyboard = []
        total_registered = 0
        has_queues = False
        
        logger.info(f"Checking groups for admin_clear. Total groups: {len(queue_manager.groups)}")
        
        for group_id, group_info in queue_manager.groups.items():
            # Check if user has admin access to this group (convert group_id to int)
            try:
                group_id_int = int(group_id)
            except (ValueError, TypeError):
                continue
                
            if queue_manager.has_admin_access(user_id, group_id_int):
                logger.info(f"User {user_id} has admin access to group {group_id}")
                group_name = group_info.get('name', f'Group {group_id}')
                
                # Count non-empty queues in this group
                group_queue_count = 0
                group_courses = queue_manager.group_courses.get(group_id, {})
                logger.info(f"Group {group_id} has {len(group_courses)} courses: {list(group_courses.keys())}")
                
                for course_id in group_courses:
                    queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                    logger.info(f"  Course {course_id} in group {group_id}: {len(queue)} registrations")
                    logger.info(f"    Queue lookup: queue_manager.group_queues.get('{group_id}', {{}}).get('{course_id}', [])")
                    logger.info(f"    Available group_queues keys: {list(queue_manager.group_queues.keys())}")
                    group_queue_count += len(queue)
                    total_registered += len(queue)
                
                logger.info(f"Group {group_id} total queue count: {group_queue_count}")
                
                if group_queue_count > 0:
                    has_queues = True
                    keyboard.append([InlineKeyboardButton(
                        f"{group_name} ({group_queue_count} total registrations)", 
                        callback_data=f"admin_clear_group_{group_id}"
                    )])

        if not has_queues:
            await update.message.reply_text("� All queues are already empty!")
            return

        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🗑️ **Clear Course Queues**\n\n"
            "Select a group to manage:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def dev_clearQ_all_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Clear all queues in all groups (global clear) with confirmation"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        # Calculate what would be cleared
        total_registrations = 0
        groups_with_data = 0
        group_details = []
        
        for group_id, group_info in queue_manager.groups.items():
            group_name = group_info.get('name', f'Group {group_id}')
            group_registrations = 0
            
            group_courses = queue_manager.get_group_courses(group_id)
            for course_id in group_courses:
                queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                group_registrations += len(queue)
            
            if group_registrations > 0:
                groups_with_data += 1
                total_registrations += group_registrations
                group_details.append(f"• {group_name}: {group_registrations} registrations")
        
        if total_registrations == 0:
            await update.message.reply_text("ℹ️ All queues are already empty - nothing to clear!")
            return
        
        # Show confirmation dialog with detailed explanation
        keyboard = [
            [InlineKeyboardButton("🗑️ YES, CLEAR EVERYTHING", callback_data="dev_clearQ_all_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        group_list = "\n".join(group_details[:5])  # Show first 5 groups
        if len(group_details) > 5:
            group_list += f"\n... and {len(group_details) - 5} more groups"
        
        message = (
            f"⚠️ **DANGER: Global Queue Clear**\n\n"
            f"**This will permanently delete ALL student registrations from ALL groups!**\n\n"
            f"**Impact:**\n"
            f"• **{total_registrations} total registrations** will be lost\n"
            f"• **{groups_with_data} groups** will be affected\n\n"
            f"**Groups with data:**\n{group_list}\n\n"
            f"**⚠️ THIS CANNOT BE UNDONE!**\n"
            f"All students will need to re-register for their courses.\n\n"
            f"Only confirm if you are absolutely sure!"
        )
        
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def admin_status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Detailed status"""
        user_id = update.effective_user.id
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return
        
        # Show group selection first (filter by admin access)
        keyboard = []
        total_registered = 0
        
        for group_id, group_info in queue_manager.groups.items():
            # Check if user has admin access to this group (convert group_id to int)
            try:
                group_id_int = int(group_id)
            except (ValueError, TypeError):
                continue
                
            if queue_manager.has_admin_access(user_id, group_id_int):
                group_name = group_info.get('name', f'Group {group_id}')
                
                # Count total registrations in this group
                group_queue_count = 0
                group_courses = queue_manager.group_courses.get(group_id, {})
                for course_id in group_courses:
                    queue = queue_manager.group_queues.get(group_id, {}).get(course_id, [])
                    group_queue_count += len(queue)
                    
                total_registered += group_queue_count
                keyboard.append([InlineKeyboardButton(
                    f"{group_name} ({group_queue_count} total registrations)", 
                    callback_data=f"admin_status_group_{group_id}"
                )])

        if not keyboard:
            await update.message.reply_text("❌ No groups found or no admin access!")
            return

        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📊 **Detailed Queue Status**\n\n"
            "Select a group to view detailed status:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def admin_config_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Show current configuration"""
        user_id = update.effective_user.id
        
        # Check admin access - either dev or group admin
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return
        
        config_text = "⚙️ **Bot Configuration**\n\n"
        
        # If user is a dev, show all groups; if group admin, show only their groups
        if queue_manager.is_dev(user_id):
            admin_groups = list(queue_manager.groups.keys())
            config_text += "**🔧 Developer View - All Groups**\n\n"
        else:
            admin_groups = queue_manager.get_admin_groups(user_id)
            if not admin_groups:
                await update.message.reply_text("❌ No groups found for your admin access.")
                return
            config_text += "**👥 Group Admin View**\n\n"
        
        # Show courses for admin's groups only
        total_courses = 0
        open_courses = 0
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        
        for group_id in admin_groups:
            # Convert string group_id to int for proper data access
            group_id_int = int(group_id) if isinstance(group_id, str) else group_id
            
            # Use the integer group ID for lookup since groups dict uses integer keys
            group_info = queue_manager.groups.get(group_id_int, {})
            
            if isinstance(group_info, dict):
                group_name = group_info.get('name', f'Group - {group_id}')
                # Get courses from the correct data structures
                group_courses_names = queue_manager.group_courses.get(group_id_int, {})
                group_schedules = queue_manager.group_schedules.get(group_id_int, {})
            else:
                group_name = f'Group - {group_id}'
                group_courses_names = {}
                group_schedules = {}
            
            if group_courses_names:
                config_text += f"**📚 Courses in {group_name}:**\n"
                for course_id, course_name in group_courses_names.items():
                    status = queue_manager.get_course_registration_status_compat(course_id)
                    schedule = group_schedules.get(course_id, {"day": 2, "time": "20:00"})
                    day_name = day_names[schedule.get('day', 2)]
                    time_str = schedule.get('time', '20:00')
                    config_text += f"  • `{course_id}` → {course_name} {status}\n"
                    config_text += f"    📅 Schedule: {day_name} {time_str}\n"
                    total_courses += 1
                    if queue_manager.is_course_registration_open_compat(course_id):
                        open_courses += 1
                config_text += "\n"
        
        # Show group admins for admin's groups only
        config_text += f"**👑 Group Admins:**\n"
        for group_id in admin_groups:
            # Convert to int for groups lookup, but use string for group_admins lookup
            group_id_int = int(group_id) if isinstance(group_id, str) else group_id
            
            group_info = queue_manager.groups.get(group_id_int, {})
            if isinstance(group_info, dict):
                group_name = group_info.get('name', f'Group - {group_id}')
            else:
                group_name = f'Group - {group_id}'
            
            admin_ids = queue_manager.group_admins.get(group_id, [])  # group_admins uses string keys
            
            if admin_ids:
                config_text += f"  • **{group_name}**:\n"
                for admin_id in admin_ids:
                    admin_name = await self.get_user_display_name(admin_id)
                    config_text += f"    - {admin_name}\n"
            else:
                config_text += f"  • **{group_name}**: No admins configured\n"
        
        # Settings summary
        config_text += f"\n**⚙️ Settings:**\n"
        # Show queue sizes for admin's groups
        for group_id in admin_groups:
            group_id_int = int(group_id) if isinstance(group_id, str) else group_id
            group_info = queue_manager.groups.get(group_id_int, {})
            if isinstance(group_info, dict):
                group_name = group_info.get('name', f'Group - {group_id}')
            else:
                group_name = f'Group - {group_id}'
            queue_size = queue_manager.get_group_queue_size(group_id_int)
            config_text += f"  • **{group_name}** queue size: {queue_size}\n"
            
        if total_courses > 0:
            config_text += f"  • Registration Status: {open_courses}/{total_courses} courses open\n"
        else:
            config_text += f"  • No courses configured in your groups\n"
        
        await update.message.reply_text(config_text, parse_mode='Markdown')
    
    async def admin_queuesize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Set queue size for group"""
        user_id = update.effective_user.id
        
        # Check admin access
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return
        
        # Parse command arguments
        if not context.args or len(context.args) != 1:
            await update.message.reply_text(
                "❌ **Usage:** `/admin_queuesize <number>`\n\n"
                "**Example:** `/admin_queuesize 30`\n"
                "Set the maximum queue size for your group(s).",
                parse_mode='Markdown'
            )
            return
        
        try:
            new_size = int(context.args[0])
            if new_size <= 0:
                await update.message.reply_text("❌ Queue size must be a positive number (greater than 0).")
                return
            if new_size > 1000:
                await update.message.reply_text("❌ Queue size cannot exceed 1000.")
                return
        except ValueError:
            await update.message.reply_text("❌ Invalid number. Please provide a valid positive integer.")
            return
        
        # Determine group context
        group_id = None
        if update.message.chat.type in ['group', 'supergroup']:
            group_id = update.message.chat.id
        
        # If in a group, check if user has admin access to that specific group
        if group_id:
            if not queue_manager.has_admin_access(user_id, group_id):
                await update.message.reply_text("❌ You don't have admin access to this group.")
                return
            
            # Set queue size for this specific group
            if queue_manager.set_group_queue_size(group_id, new_size):
                group_info = queue_manager.groups.get(group_id, {})
                group_name = group_info.get('name', f'Group {group_id}')
                await update.message.reply_text(
                    f"✅ **Queue size updated successfully!**\n\n"
                    f"**Group:** {group_name}\n"
                    f"**New queue size:** {new_size}",
                    parse_mode='Markdown'
                )
                logger.info(f"Queue size for group {group_id} ({group_name}) set to {new_size} by user {user_id}")
            else:
                await update.message.reply_text("❌ Failed to update queue size.")
        else:
            # Private message - need to select which group
            admin_groups = queue_manager.get_admin_groups(user_id)
            if not admin_groups:
                await update.message.reply_text("❌ No groups found for your admin access.")
                return
            
            if len(admin_groups) == 1:
                # Only one group, set it directly
                group_id_str = admin_groups[0]
                group_id_int = int(group_id_str) if isinstance(group_id_str, str) else group_id_str
                
                if queue_manager.set_group_queue_size(group_id_int, new_size):
                    group_info = queue_manager.groups.get(group_id_int, {})
                    group_name = group_info.get('name', f'Group {group_id_str}')
                    await update.message.reply_text(
                        f"✅ **Queue size updated successfully!**\n\n"
                        f"**Group:** {group_name}\n"
                        f"**New queue size:** {new_size}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"Queue size for group {group_id_int} ({group_name}) set to {new_size} by user {user_id}")
                else:
                    await update.message.reply_text("❌ Failed to update queue size.")
            else:
                # Multiple groups - redirect to dev command
                await update.message.reply_text(
                    "❌ **Multiple group management requires dev privileges.**\n\n"
                    "Please use `/dev_queuesize` command for managing multiple groups.",
                    parse_mode='Markdown'
                )

    async def dev_queuesize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Set queue size for any group"""
        user_id = update.effective_user.id
        
        # Check dev access
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        # Parse command arguments
        if not context.args or len(context.args) != 1:
            await update.message.reply_text(
                "❌ **Usage:** `/dev_queuesize <number>`\n\n"
                "**Example:** `/dev_queuesize 30`\n"
                "Set the maximum queue size for any group.",
                parse_mode='Markdown'
            )
            return
        
        try:
            new_size = int(context.args[0])
            if new_size <= 0:
                await update.message.reply_text("❌ Queue size must be a positive number (greater than 0).")
                return
            if new_size > 1000:
                await update.message.reply_text("❌ Queue size cannot exceed 1000.")
                return
        except ValueError:
            await update.message.reply_text("❌ Invalid number. Please provide a valid positive integer.")
            return
        
        # Determine group context
        group_id = None
        if update.message.chat.type in ['group', 'supergroup']:
            group_id = update.message.chat.id
        
        # If in a group, set queue size for that group
        if group_id:
            if queue_manager.set_group_queue_size(group_id, new_size):
                group_info = queue_manager.groups.get(group_id, {})
                group_name = group_info.get('name', f'Group {group_id}')
                await update.message.reply_text(
                    f"✅ **Queue size updated successfully!**\n\n"
                    f"**Group:** {group_name}\n"
                    f"**New queue size:** {new_size}",
                    parse_mode='Markdown'
                )
                logger.info(f"Queue size for group {group_id} ({group_name}) set to {new_size} by dev user {user_id}")
            else:
                await update.message.reply_text("❌ Failed to update queue size.")
        else:
            # Private message - show all groups for selection
            if not queue_manager.groups:
                await update.message.reply_text("❌ No groups found in the system.")
                return
            
            if len(queue_manager.groups) == 1:
                # Only one group, set it directly
                group_id_int = next(iter(queue_manager.groups.keys()))
                
                if queue_manager.set_group_queue_size(group_id_int, new_size):
                    group_info = queue_manager.groups.get(group_id_int, {})
                    group_name = group_info.get('name', f'Group {group_id_int}')
                    await update.message.reply_text(
                        f"✅ **Queue size updated successfully!**\n\n"
                        f"**Group:** {group_name}\n"
                        f"**New queue size:** {new_size}",
                        parse_mode='Markdown'
                    )
                    logger.info(f"Queue size for group {group_id_int} ({group_name}) set to {new_size} by dev user {user_id}")
                else:
                    await update.message.reply_text("❌ Failed to update queue size.")
            else:
                # Multiple groups - create selection keyboard
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                
                keyboard = []
                for group_id_int, group_info in queue_manager.groups.items():
                    group_name = group_info.get('name', f'Group {group_id_int}')
                    current_size = queue_manager.get_group_queue_size(group_id_int)
                    
                    # Store the new size in user state for the callback
                    callback_data = f"dev_queuesize_{group_id_int}_{new_size}"
                    keyboard.append([InlineKeyboardButton(
                        f"📊 {group_name} (current: {current_size})", 
                        callback_data=callback_data
                    )])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"📊 **Select group to set queue size to {new_size}:**",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )

    async def admin_swap_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Swap positions in queue"""
        user_id = update.effective_user.id
        
        # Debug logging
        logger.info(f"admin_swap_command called by user {user_id}")
        logger.info(f"User {user_id} - is_dev: {queue_manager.is_dev(user_id)}, is_admin: {queue_manager.is_admin(user_id)}")
        logger.info(f"User {user_id} - has_admin_access (no group): {queue_manager.has_admin_access(user_id)}")
        
        # Determine group context
        if update.message.chat.type in ['group', 'supergroup']:
            group_id = update.message.chat.id
            logger.info(f"admin_swap_command in group {group_id}")
            logger.info(f"User {user_id} - has_admin_access (group {group_id}): {queue_manager.has_admin_access(user_id, group_id)}")
            # Check if user has admin access to this specific group
            if not queue_manager.has_admin_access(user_id, group_id):
                await update.message.reply_text("❌ Доступ запрещён. У вас нет прав администратора в этой группе.")
                return
            await self.show_swap_courses_for_group(update, group_id)
        else:
            # Private chat - show group selection first
            logger.info(f"admin_swap_command in private chat")
            if not queue_manager.has_admin_access(user_id):
                logger.info(f"User {user_id} does not have admin access - denying")
                await update.message.reply_text("❌ Доступ запрещён. Требуются права администратора.")
                return
            
            if not queue_manager.groups:
                await update.message.reply_text("❌ Группы не настроены.")
                return
            
            keyboard = []
            for group_id, group_info in queue_manager.groups.items():
                # Check if user has admin access to this group
                logger.info(f"Checking admin access for user {user_id} to group {group_id} (type: {type(group_id)})")
                # Convert group_id to int for consistent checking
                try:
                    group_id_int = int(group_id)
                except (ValueError, TypeError):
                    logger.error(f"Cannot convert group_id {group_id} to int")
                    continue
                    
                if queue_manager.has_admin_access(user_id, group_id_int):
                    logger.info(f"User {user_id} has access to group {group_id_int}")
                    group_name = group_info.get('name', f'Group {group_id}')
                    keyboard.append([InlineKeyboardButton(
                        f"{group_name}", 
                        callback_data=f"admin_swap_group_{group_id}"
                    )])
                else:
                    logger.info(f"User {user_id} does NOT have access to group {group_id_int}")
            
            if not keyboard:
                logger.info(f"User {user_id} has no admin rights to any group")
                await update.message.reply_text("У вас нет прав администратора ни в одной группе.")
                return
            
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🔄 **Админ: Поменять местами позиции в очереди**\n\n"
                "Выберите группу:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_swap_courses_for_group(self, update, group_id):
        """Show courses available for swapping in a specific group"""
        # Show course selection for swap
        group_courses = queue_manager.get_group_courses(group_id)
        if not group_courses:
            if hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.edit_message_text("В этой группе нет настроенных курсов.")
            else:
                await update.message.reply_text("В этой группе нет настроенных курсов.")
            return
        
        keyboard = []
        for course_id, course_name in group_courses.items():
            queue_entries = queue_manager.group_queues[group_id][course_id]
            queue_size = len(queue_entries)
            if queue_size < 2:
                # Skip courses with less than 2 registrations
                continue
            keyboard.append([InlineKeyboardButton(
                f"{course_name} ({queue_size} записей)", 
                callback_data=f"admin_swap_{course_id}"
            )])
        
        if not keyboard:
            if hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.edit_message_text("В этой группе нет курсов с 2 или более записями.")
            else:
                await update.message.reply_text("В этой группе нет курсов с 2 или более записями.")
            return
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            "🔄 **Админ: Поменять местами позиции в очереди**\n\n"
            "Выберите курс:"
        )
        
        if hasattr(update, 'callback_query') and update.callback_query:
            # Store group_id in context for callback
            # We need to access context from the callback query
            await update.callback_query.answer()
            # Store group_id via user state since we can't access context directly
            self.set_user_state(update.effective_user.id, 'swap_group_selected', {'group_id': group_id})
            
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            # Direct message context - store group_id for later use
            self.set_user_state(update.effective_user.id, 'swap_group_selected', {'group_id': group_id})
            
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    
    async def show_swap_interface(self, query, course_id, context):
        """Show the queue and ask for positions to swap"""
        # Get group_id from user_data or user state
        group_id = context.user_data.get('swap_group_id')
        if not group_id:
            # Try to get from user state
            user_id = query.from_user.id
            user_state = self.user_states.get(user_id)
            if user_state and user_state.get('state') == 'swap_group_selected':
                group_id = user_state.get('data', {}).get('group_id')
        
        if not group_id:
            await query.edit_message_text("❌ Ошибка: Информация о группе потеряна. Попробуйте снова.")
            return
            
        group_courses = queue_manager.get_group_courses(group_id)
        course_name = group_courses.get(course_id)
        if not course_name:
            await query.edit_message_text("❌ Курс не найден в этой группе.")
            return
            
        queue_entries = queue_manager.group_queues[group_id][course_id]
        
        if len(queue_entries) < 2:
            await query.edit_message_text("В этом курсе недостаточно записей для обмена (минимум 2).")
            return
        
        # Build queue display in Russian
        message = f"🔄 **Обмен позициями - {course_name}**\n\n"
        message += "**Текущая очередь:**\n"
        
        for i, entry in enumerate(queue_entries, 1):
            message += f"{i:2d}. {entry['full_name']}\n"
        
        message += f"\n**Инструкции:**\n"
        message += f"Ответьте двумя номерами позиций для обмена (например, '3 5' для обмена позиций 3 и 5)\n"
        message += f"Допустимые позиции: от 1 до {len(queue_entries)}"
        
        # Store course_id and group_id in user data for message handler
        context.user_data.clear()
        context.user_data['awaiting_swap_positions'] = True
        context.user_data['swap_course_id'] = course_id
        context.user_data['swap_group_id'] = group_id
        
        # Also clear the user state since we now have the info in context
        self.clear_user_state(query.from_user.id)
        
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
        # Store course_id and group_id in user data for message handler
        context.user_data.clear()
        context.user_data['awaiting_swap_positions'] = True
        context.user_data['swap_course_id'] = course_id
        context.user_data['swap_group_id'] = group_id
        
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def process_swap_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE, positions_text: str):
        """Process the swap positions input"""
        course_id = context.user_data.get('swap_course_id')
        group_id = context.user_data.get('swap_group_id')
        if not course_id or not group_id:
            await update.message.reply_text("❌ Ошибка: Информация о курсе или группе потеряна. Попробуйте снова.")
            return
        
        # Parse positions
        try:
            positions = positions_text.strip().split()
            if len(positions) != 2:
                await update.message.reply_text("❌ Укажите ровно два номера позиций (например, '1 3').")
                return
            
            pos1 = int(positions[0])
            pos2 = int(positions[1])
        except ValueError:
            await update.message.reply_text("❌ Неверный ввод. Используйте только числа (например, '1 3').")
            return
        
        # Validate positions
        queue_entries = queue_manager.group_queues[group_id][course_id]
        if pos1 < 1 or pos1 > len(queue_entries) or pos2 < 1 or pos2 > len(queue_entries):
            await update.message.reply_text(f"❌ Номера позиций должны быть от 1 до {len(queue_entries)}.")
            return
        
        if pos1 == pos2:
            await update.message.reply_text("❌ Нельзя поменять позицию саму с собой. Выберите разные позиции.")
            return
        
        # Perform the swap
        group_courses = queue_manager.get_group_courses(group_id)
        course_name = group_courses.get(course_id, course_id)
        entry1 = queue_entries[pos1 - 1].copy()
        entry2 = queue_entries[pos2 - 1].copy()
        
        # Swap the entries
        queue_entries[pos1 - 1] = entry2
        queue_entries[pos2 - 1] = entry1
        
        # Update positions in the entries
        queue_entries[pos1 - 1]['position'] = pos1
        queue_entries[pos2 - 1]['position'] = pos2
        
        # Save changes
        queue_manager.save_data()
        
        # Show success message with new queue in Russian
        message = f"✅ **Обмен завершён - {course_name}**\n\n"
        message += f"**Обменены:**\n"
        message += f"Позиция {pos1}: {entry2['full_name']}\n"
        message += f"Позиция {pos2}: {entry1['full_name']}\n\n"
        message += f"**Обновлённая очередь:**\n"
        
        for i, entry in enumerate(queue_entries, 1):
            swap_marker = ""
            if i == pos1 or i == pos2:
                swap_marker = " 🔄"
            message += f"{i:2d}. {entry['full_name']}{swap_marker}\n"
        
        # Clear user data
        context.user_data.clear()
        
        await update.message.reply_text(message, parse_mode='Markdown')
        
        # Update positions in the entries
        queue_entries[pos1 - 1]['position'] = pos1
        queue_entries[pos2 - 1]['position'] = pos2
        
        # Save changes using the new group-based structure
        queue_manager.save_data()
        
        # Show success message with new queue
        message = f"✅ **Swap Completed - {course_name}**\n\n"
        message += f"**Swapped:**\n"
        message += f"Position {pos1}: {entry1['full_name']}\n"
        message += f"Position {pos2}: {entry2['full_name']}\n\n"
        message += f"**Updated Queue:**\n"
        
        for i, entry in enumerate(queue_entries, 1):
            swap_marker = ""
            if i == pos1 or i == pos2:
                swap_marker = " 🔄"
            message += f"{i:2d}. {entry['full_name']}{swap_marker}\n"
        
        # Clear user data
        context.user_data.clear()
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def admin_add_course_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Add a new course"""
        user_id = update.effective_user.id
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Доступ запрещён. Требуются права администратора.")
            return
        
        # Determine group context
        if update.message.chat.type in ['group', 'supergroup']:
            group_id = update.message.chat.id
            # Check if user has admin access to this specific group
            if not queue_manager.has_admin_access(user_id, group_id):
                await update.message.reply_text("❌ Доступ запрещён. У вас нет прав администратора в этой группе.")
                return
            
            # Start the add course conversation with group context
            self.set_user_state(user_id, 'add_course_id', {'group_id': group_id})
            
            await update.message.reply_text(
                "➕ **Добавить новый курс**\n\n"
                f"Добавление курса в: {update.message.chat.title}\n\n"
                "Шаг 1/4: Введите ID курса (короткий идентификатор, строчными буквами, например, 'math101', 'phys201'):\n\n"
                "Введите ID курса или отправьте /cancel для отмены.",
                parse_mode='Markdown'
            )
        else:
            # Private chat - show group selection first
            keyboard = []
            
            for group_id, group_info in queue_manager.groups.items():
                # Check if user has admin access to this group
                if queue_manager.has_admin_access(user_id, group_id):
                    group_name = group_info.get('name', f'Group {group_id}')
                    course_count = len(queue_manager.group_courses.get(group_id, {}))
                    keyboard.append([InlineKeyboardButton(
                        f"{group_name} ({course_count} courses)", 
                        callback_data=f"admin_add_course_group_{group_id}"
                    )])

            if not keyboard:
                await update.message.reply_text("❌ Группы не найдены или нет доступа администратора!")
                return

            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "➕ **Добавить новый курс**\n\n"
                "Выберите группу для добавления курса:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    
    async def dev_add_admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Add admin to a specific group"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        # Show group selection for adding admin to
        keyboard = []
        for group_id, group_info in queue_manager.groups.items():
            group_name = group_info.get('name', f'Group {group_id}')
            # Convert group_id to string to match group_admins keys
            admin_count = len(queue_manager.group_admins.get(str(group_id), []))
            keyboard.append([InlineKeyboardButton(
                f"{group_name} ({admin_count} admins)", 
                callback_data=f"dev_add_admin_group_{group_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👑 **Add Admin to Group**\n\n"
            "Select a group to add admin to:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def dev_list_admins_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: List all admins across groups"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        message_text = "👑 **Admin Overview**\n\n"
        
        # Show dev users
        if queue_manager.dev_users:
            message_text += "🔥 **Dev Users (Global Access):**\n"
            for dev_user in queue_manager.dev_users:
                dev_name = await self.get_user_display_name(dev_user)
                message_text += f"• {dev_name} (`{dev_user}`)\n"
            message_text += "\n"
        
        # Show group admins
        message_text += "👥 **Group Admins:**\n"
        for group_id_str, admin_list in queue_manager.group_admins.items():
            # Convert string group_id to int to match groups dictionary
            try:
                group_id = int(group_id_str)
                group_info = queue_manager.groups.get(group_id, {})
                group_name = group_info.get('name', f'Group {group_id}')
            except (ValueError, TypeError):
                group_name = f'Group {group_id_str}'
                
            if admin_list:
                message_text += f"**{group_name}:**\n"
                for admin_user in admin_list:
                    admin_name = await self.get_user_display_name(admin_user)
                    message_text += f"  • {admin_name} (`{admin_user}`)\n"
            else:
                message_text += f"**{group_name}:** No admins\n"
        
        await update.message.reply_text(message_text, parse_mode='Markdown')
    
    async def dev_remove_admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Remove admin from a specific group"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        # Show groups with admins
        keyboard = []
        for group_id_str, admin_list in queue_manager.group_admins.items():
            if admin_list:  # Only show groups that have admins
                # Convert string group_id to int to match groups dictionary
                try:
                    group_id = int(group_id_str)
                    group_info = queue_manager.groups.get(group_id, {})
                    group_name = group_info.get('name', f'Group {group_id}')
                except (ValueError, TypeError):
                    group_name = f'Group {group_id_str}'
                    
                admin_count = len(admin_list)
                keyboard.append([InlineKeyboardButton(
                    f"{group_name} ({admin_count} admins)", 
                    callback_data=f"dev_remove_admin_group_{group_id_str}"
                )])
        
        if not keyboard:
            await update.message.reply_text("ℹ️ No group admins to remove.")
            return
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🗑️ **Remove Admin from Group**\n\n"
            "Select a group to remove admin from:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def dev_cleanup_groups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Clean up stale groups where bot is no longer a member"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        await update.message.reply_text("🔍 Checking bot membership in all groups... Please wait.")
        
        stale_groups = []
        active_groups = []
        
        # Check each group
        for group_id, group_info in queue_manager.groups.items():
            group_name = group_info.get('name', f'Group {group_id}')
            # Convert group_id to int for the API call
            try:
                group_id_int = int(group_id)
                is_member = await queue_manager.check_bot_in_group(self.application.bot, group_id_int)
            except (ValueError, TypeError):
                logger.error(f"Invalid group_id format: {group_id}")
                is_member = False
            
            if is_member:
                active_groups.append((group_id, group_name))
            else:
                course_count = len(queue_manager.group_courses.get(group_id, {}))
                queue_count = sum(len(q) for q in queue_manager.group_queues.get(group_id, {}).values())
                stale_groups.append((group_id, group_name, course_count, queue_count))
        
        if not stale_groups:
            message = "✅ **All Groups Active**\n\n"
            message += f"Bot is active in all {len(active_groups)} configured groups:\n"
            for group_id, group_name in active_groups:
                message += f"• {group_name} ({group_id})\n"
        else:
            # Show stale groups with cleanup options
            message = f"⚠️ **Found {len(stale_groups)} Stale Groups**\n\n"
            message += "These groups have data but bot is no longer a member:\n\n"
            
            keyboard = []
            for group_id, group_name, course_count, queue_count in stale_groups:
                message += f"🔴 **{group_name}** ({group_id})\n"
                message += f"   • {course_count} courses, {queue_count} registrations\n\n"
                
                keyboard.append([InlineKeyboardButton(
                    f"🗑️ Remove {group_name}",
                    callback_data=f"dev_cleanup_group_{group_id}"
                )])
            
            message += f"✅ **Active groups**: {len(active_groups)}\n"
            for group_id, group_name in active_groups[:3]:  # Show first 3
                message += f"• {group_name}\n"
            if len(active_groups) > 3:
                message += f"... and {len(active_groups) - 3} more\n"
            
            keyboard.append([InlineKeyboardButton("🗑️ Remove All Stale", callback_data="dev_cleanup_all_stale")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
            return
        
        await update.message.reply_text(message, parse_mode='Markdown')

    async def dev_test_group_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Test bot membership in a specific group"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /dev_test_group <group_id>\nExample: /dev_test_group -4734662699")
            return
            
        try:
            group_id = int(context.args[0])
            await update.message.reply_text(f"🔍 Testing bot membership in group {group_id}...")
            
            # Enhanced debugging - try multiple methods to check group status
            try:
                # Method 1: Check bot member status
                bot_member = await self.application.bot.get_chat_member(group_id, self.application.bot.id)
                bot_status = bot_member.status
                status_details = f"Status: '{bot_status}'"
            except Exception as e:
                bot_status = "ERROR"
                status_details = f"Error getting status: {type(e).__name__}: {str(e)}"
            
            try:
                # Method 2: Try to get chat info
                chat_info = await self.application.bot.get_chat(group_id)
                chat_details = f"Chat exists: {chat_info.title} (type: {chat_info.type})"
            except Exception as e:
                chat_details = f"Chat error: {type(e).__name__}: {str(e)}"
            
            # Use our standard check method
            is_member = await queue_manager.check_bot_in_group(self.application.bot, group_id)
            
            # Check if group exists in config
            group_in_config = group_id in queue_manager.groups
            group_name = queue_manager.groups.get(group_id, {}).get('name', 'Unknown')
            
            result = f"**Group {group_id} ({group_name})**\n\n"
            result += f"✅ In config: {group_in_config}\n"
            result += f"🤖 Bot is member: {is_member}\n\n"
            result += f"**Debug Info:**\n"
            result += f"• {status_details}\n"
            result += f"• {chat_details}\n\n"
            
            if group_in_config and not is_member:
                result += "⚠️ **This group appears to be stale** (in config but bot not member)"
            elif not group_in_config and is_member:
                result += "ℹ️ **Bot is member but group not in config**"
            elif group_in_config and is_member:
                result += "✅ **Group is active and properly configured**"
            else:
                result += "❌ **Group not found anywhere**"
            
            await update.message.reply_text(result, parse_mode='Markdown')
            
        except ValueError:
            await update.message.reply_text("❌ Invalid group ID. Must be a number (negative for groups)")
        except Exception as e:
            await update.message.reply_text(f"❌ Error testing group: {e}")

    async def dev_blacklist_add_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Add a user to the blacklist"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        if len(context.args) != 1:
            await update.message.reply_text(
                "**Usage:** /dev_blacklist_add <user_id>\n"
                "**Example:** /dev_blacklist_add 123456789\n\n"
                "💡 *Users can find their ID by messaging @userinfobot*",
                parse_mode='Markdown'
            )
            return
        
        try:
            target_user_id = int(context.args[0])
            
            # Prevent blacklisting devs
            if queue_manager.is_dev(target_user_id):
                await update.message.reply_text("❌ Cannot blacklist a dev user.")
                return
            
            success, message = queue_manager.add_to_blacklist(target_user_id)
            await update.message.reply_text(message)
            
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Must be a number.")

    async def dev_blacklist_remove_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: Remove a user from the blacklist"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        if len(context.args) != 1:
            await update.message.reply_text(
                "**Usage:** /dev_blacklist_remove <user_id>\n"
                "**Example:** /dev_blacklist_remove 123456789",
                parse_mode='Markdown'
            )
            return
        
        try:
            target_user_id = int(context.args[0])
            success, message = queue_manager.remove_from_blacklist(target_user_id)
            await update.message.reply_text(message)
            
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Must be a number.")

    async def dev_blacklist_list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dev: List all blacklisted users"""
        user_id = update.effective_user.id
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            return
        
        blacklist = queue_manager.get_blacklist()
        
        if not blacklist:
            await update.message.reply_text("📋 The blacklist is empty.")
            return
        
        message = f"🚫 **Blacklisted Users ({len(blacklist)}):**\n\n"
        for bl_user_id in blacklist:
            # Try to get user info
            try:
                user_name = await self.get_user_display_name(bl_user_id)
                message += f"• {user_name} (ID: `{bl_user_id}`)\n"
            except:
                message += f"• User ID: `{bl_user_id}`\n"
        
        message += f"\n💡 *Use /dev_blacklist_remove <user_id> to remove a user from the blacklist*"
        
        await update.message.reply_text(message, parse_mode='Markdown')

    async def admin_remove_course_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin: Remove a course"""
        user_id = update.effective_user.id
        if not queue_manager.has_admin_access(user_id):
            await update.message.reply_text("❌ Access denied. Admin privileges required.")
            return

        # Show group selection first (filter by admin access)
        keyboard = []
        has_courses = False
        for group_id, group_info in queue_manager.groups.items():
            # Check if user has admin access to this group
            if queue_manager.has_admin_access(user_id, group_id):
                group_name = group_info.get('name', f'Group {group_id}')
                group_courses = queue_manager.group_courses.get(group_id, {})
                course_count = len(group_courses)
                if course_count > 0:
                    has_courses = True
                    keyboard.append([InlineKeyboardButton(
                        f"{group_name} ({course_count} courses)", 
                        callback_data=f"admin_remove_group_{group_id}"
                    )])

        if not has_courses:
            await update.message.reply_text("❌ No courses to remove in groups you have access to.")
            return

        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🗑️ **Remove Course**\n\n"
            "⚠️ **Warning**: This will permanently delete the course and all its data.\n"
            "Courses with registered students cannot be removed.\n\n"
            "Select a group to manage:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def handle_add_course_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle multi-step add course conversation"""
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        state = user_state.get('state')
        data = user_state.get('data', {})
        
        if state == 'add_course_id':
            # Step 1: Get course ID
            course_id = update.message.text.strip().lower()
            
            if not course_id:
                await update.message.reply_text("❌ ID курса не может быть пустым. Попробуйте снова:")
                return
            
            # Check if course exists in the specific group
            group_id = data.get('group_id')
            if group_id and course_id in queue_manager.group_courses.get(group_id, {}):
                await update.message.reply_text(f"❌ ID курса '{course_id}' уже существует в этой группе. Пожалуйста, выберите другой ID:")
                return
            
            data['course_id'] = course_id
            self.set_user_state(user_id, 'add_course_name', data)
            
            await update.message.reply_text(
                f"✅ ID курса: `{course_id}`\n\n"
                "Шаг 2/4: Введите отображаемое название курса (например, 'Математика 101', 'Лаборатория физики'):",
                parse_mode='Markdown'
            )
        
        elif state == 'add_course_name':
            # Step 2: Get course name
            course_name = update.message.text.strip()
            
            if not course_name:
                await update.message.reply_text("❌ Название курса не может быть пустым. Попробуйте снова:")
                return
            
            data['course_name'] = course_name
            self.set_user_state(user_id, 'add_course_day', data)
            
            # Show day selection keyboard
            keyboard = [
                [InlineKeyboardButton("Понедельник", callback_data="add_day_0"),
                 InlineKeyboardButton("Вторник", callback_data="add_day_1")],
                [InlineKeyboardButton("Среда", callback_data="add_day_2"),
                 InlineKeyboardButton("Четверг", callback_data="add_day_3")],
                [InlineKeyboardButton("Пятница", callback_data="add_day_4"),
                 InlineKeyboardButton("Суббота", callback_data="add_day_5")],
                [InlineKeyboardButton("Воскресенье", callback_data="add_day_6")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ ID курса: `{data['course_id']}`\n"
                f"✅ Название курса: {course_name}\n\n"
                "Шаг 3/4: Выберите день, когда открывается регистрация:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        
        elif state == 'add_course_time':
            # Step 4: Get time
            time_text = update.message.text.strip()
            
            # Validate time format
            try:
                time_obj = datetime.strptime(time_text, "%H:%M").time()
            except ValueError:
                await update.message.reply_text(
                    "❌ Неверный формат времени. Пожалуйста, используйте формат ЧЧ:ММ (например, '18:00', '09:30'):"
                )
                return
            
            data['time'] = time_text
            
            # Confirm and create course
            day_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
            day_name = day_names[data['day']]
            
            # Get group_id from the original message context or user's associated group
            group_id = data.get('group_id')
            if not group_id:
                # If no group_id stored in conversation data, try to get from update context
                if update.message.chat.type in ['group', 'supergroup']:
                    group_id = update.message.chat.id
                else:
                    # For private chats, we need to determine which group to add to
                    await update.message.reply_text(
                        "❌ Невозможно определить, в какую группу добавить курс. "
                        "Пожалуйста, используйте эту команду в группе, где вы хотите добавить курс."
                    )
                    self.clear_user_state(user_id)
                    return
            
            success, message = await queue_manager.add_course(
                group_id,
                data['course_id'], 
                data['course_name'], 
                data['day'], 
                data['time'], 
                self
            )
            
            if success:
                confirmation_msg = (
                    f"✅ Курс успешно добавлен!\n\n"
                    f"Детали:\n"
                    f"• ID: {data['course_id']}\n"
                    f"• Название: {data['course_name']}\n"
                    f"• Расписание: {day_name} в {data['time']}\n"
                    f"• Статус: 🔴 Закрыт (используйте /admin_open для открытия регистрации)\n\n"
                    f"{message}"
                )
            else:
                confirmation_msg = f"❌ Не удалось добавить курс\n\n{message}"
            
            self.clear_user_state(user_id)
            await update.message.reply_text(confirmation_msg)  # Use plain text for now
    
    async def handle_dev_add_admin_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle dev add admin conversation"""
        user_id = update.effective_user.id
        user_state = self.get_user_state(user_id)
        data = user_state.get('data', {})
        group_id = data.get('group_id')
        
        if not queue_manager.is_dev(user_id):
            await update.message.reply_text("❌ Access denied. Dev privileges required.")
            self.clear_user_state(user_id)
            return
        
        # Parse the user ID from message
        try:
            new_admin_id = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid User ID format. Please send a valid numeric User ID.\n\n"
                "💡 *Tip: Users can find their ID by messaging @userinfobot*",
                parse_mode='Markdown'
            )
            return
        
        # Check if user is already admin of this group (use string key)
        group_id_str = str(group_id)
        if new_admin_id in queue_manager.group_admins.get(group_id_str, []):
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await update.message.reply_text(f"ℹ️ User {new_admin_id} is already an admin of {group_name}.")
            self.clear_user_state(user_id)
            return
        
        # Add admin to group (use string key for consistency)
        if group_id_str not in queue_manager.group_admins:
            queue_manager.group_admins[group_id_str] = []
        queue_manager.group_admins[group_id_str].append(new_admin_id)
        queue_manager.save_config()
        
        # Reload admin config to ensure permissions are immediately available
        queue_manager.reload_admin_config()
        
        # Convert int group_id to match groups dictionary (group_id is int here)
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        admin_name = await self.get_user_display_name(new_admin_id)
        
        # Update command suggestions for the newly added admin
        await self.setup_user_commands(new_admin_id)
        
        await update.message.reply_text(
            f"✅ **Admin Added Successfully!**\n\n"
            f"👤 Admin: {admin_name}\n"
            f"👥 Group: {group_name}\n\n"
            f"This user now has admin privileges for this group and can use admin commands.\n"
            f"Their command suggestions have been updated.",
            parse_mode='Markdown'
        )
        
        self.clear_user_state(user_id)

    async def handle_add_course_day_callback(self, query, day: int, context: ContextTypes.DEFAULT_TYPE):
        """Handle day selection for add course"""
        user_id = query.from_user.id
        user_state = self.get_user_state(user_id)
        
        if user_state.get('state') != 'add_course_day':
            await query.answer("Invalid state")
            return
        
        data = user_state.get('data', {})
        data['day'] = day
        self.set_user_state(user_id, 'add_course_time', data)
        
        day_names = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        day_name = day_names[day]
        
        await query.edit_message_text(
            f"✅ ID курса: `{data['course_id']}`\n"
            f"✅ Название курса: {data['course_name']}\n"
            f"✅ День: {day_name}\n\n"
            "Шаг 4/4: Введите время открытия регистрации (формат ЧЧ:ММ, например, '18:00'):",
            parse_mode='Markdown'
        )
        await query.answer()
    
    async def handle_remove_course_callback(self, query, group_id: int, course_id: str, context: ContextTypes.DEFAULT_TYPE):
        """Handle course removal confirmation"""
        user_id = query.from_user.id
        if not queue_manager.has_admin_access(user_id, group_id):
            await query.answer("Access denied")
            return
        
        # Validate group and course existence
        group_courses = queue_manager.get_group_courses(group_id)
        if not group_courses or course_id not in group_courses:
            await query.edit_message_text("❌ Course not found in this group.")
            await query.answer()
            return
        
        course_name = group_courses[course_id]
        
        # Check if course has registrations
        queue_size = len(queue_manager.group_queues.get(group_id, {}).get(course_id, []))
        if queue_size > 0:
            group_info = queue_manager.groups.get(group_id, {})
            group_name = group_info.get('name', f'Group {group_id}')
            await query.edit_message_text(
                f"❌ <b>Cannot Remove Course</b>\n\n"
                f"Course '{course_name}' in {group_name} has {queue_size} registered students.\n"
                f"Please clear the queue first using /admin_clear, then try again.",
                parse_mode='HTML'
            )
            await query.answer()
            return
        
        # Show confirmation dialog
        keyboard = [
            [InlineKeyboardButton("🗑️ Yes, Remove Course", callback_data=f"confirm_remove_{group_id}_{course_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Get schedule from group schedules
        schedule = queue_manager.group_schedules.get(group_id, {}).get(course_id, {"day": 2, "time": "20:00"})
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        day_name = day_names[schedule['day']]
        
        await query.edit_message_text(
            f"⚠️ <b>Confirm Course Removal</b>\n\n"
            f"<b>Course Details:</b>\n"
            f"• ID: <code>{course_id}</code>\n"
            f"• Name: {course_name}\n"
            f"• Schedule: {day_name} at {schedule['time']}\n"
            f"• Registrations: {queue_size}\n\n"
            f"<b>This action cannot be undone!</b>\n"
            f"All course data and scheduling will be permanently deleted.",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        await query.answer()
    
    async def handle_confirm_remove_callback(self, query, group_id: int, course_id: str, context: ContextTypes.DEFAULT_TYPE):
        """Handle final course removal confirmation"""
        user_id = query.from_user.id
        if not queue_manager.has_admin_access(user_id, group_id):
            await query.answer("Access denied")
            return
        
        success, message = await queue_manager.remove_course(group_id, course_id, self)
        
        if success:
            result_msg = f"✅ **Course Removed Successfully**\n\n{message}"
        else:
            result_msg = f"❌ **Failed to Remove Course**\n\n{message}"
        
        await query.edit_message_text(result_msg, parse_mode='Markdown')
        await query.answer()
    
    def get_next_registration_time(self, group_id: int = None) -> str:
        """Get next registration opening time for a specific group"""
        if not group_id:
            return "Группа не найдена"
        
        group_courses = queue_manager.get_group_courses(group_id)
        group_schedules = queue_manager.get_group_schedules(group_id)
        
        if not group_courses:
            return "Нет курсов в этой группе"
        
        now = datetime.now(TIMEZONE)
        next_openings = []
        
        for course_id, course_name in group_courses.items():
            schedule = group_schedules.get(course_id, {"day": 2, "time": "20:00"})
            reg_day = schedule['day']
            reg_time_str = schedule['time']
            
            reg_time_parts = reg_time_str.split(':')
            reg_time = time(int(reg_time_parts[0]), int(reg_time_parts[1]))
            
            # Find next occurrence of this day at registration time
            days_ahead = reg_day - now.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            
            next_date = now.date() + timedelta(days=days_ahead)
            next_datetime = datetime.combine(next_date, reg_time)
            next_datetime = TIMEZONE.localize(next_datetime)
            
            next_openings.append((next_datetime, course_name))
        
        # Get the earliest opening
        if next_openings:
            earliest = min(next_openings, key=lambda x: x[0])
            days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
            day_name = days[earliest[0].weekday()]
            return f"{day_name}, {earliest[0].strftime('%d %B в %H:%M')}"
        
        return "Расписание не найдено"
    
    async def scheduled_open_registration(self, context: ContextTypes.DEFAULT_TYPE):
        """Legacy scheduled job - no longer used with group-aware system"""
        logger.warning("Legacy scheduled_open_registration called - should use group-aware version")
    
    async def scheduled_course_registration_opener(self, group_id: int, course_id: str, context: ContextTypes.DEFAULT_TYPE = None):
        """Scheduled job to open registration for a specific course in a specific group"""
        group_courses = queue_manager.get_group_courses(group_id)
        course_name = group_courses.get(course_id, course_id)
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        logger.info(f"Opening registration for {course_name} in {group_name} via scheduled job")
        queue_manager.open_course_registration(group_id, course_id)
        if context:
            await self.notify_course_registration_open(context, group_id, course_id)
    
    async def notify_course_registration_open(self, context: ContextTypes.DEFAULT_TYPE, course_id: str):
        """Notify about course registration opening"""
        course_name = queue_manager.courses.get(course_id, course_id)
        message = (
            f"🔔 **{course_name} Registration is now OPEN!** 🔔\n\n"
            "Use /register to sign up for this course.\n"
            "Use /status to see current queues."
        )
        
        # In a real implementation, you'd send this to specific groups
        # For now, this is just a placeholder
        logger.info(f"Course registration opened: {message}")
    
    async def notify_registration_open(self, context: ContextTypes.DEFAULT_TYPE):
        """Notify about registration opening"""
        message = (
            "🔔 **Registration is now OPEN!** 🔔\n\n"
            "Use /register to sign up for courses.\n"
            "Use /status to see current queues."
        )
        
        # In a real implementation, you'd send this to specific groups
        # For now, this is just a placeholder
        logger.info("Registration opened notification sent")
    
    def setup_scheduler(self, job_queue: JobQueue):
        """Set up scheduled registration opening for each course"""
        
        # Safety check - make sure queue_manager is properly initialized
        if not hasattr(queue_manager, 'groups') or not queue_manager.groups:
            logger.warning("No groups configured, skipping scheduler setup")
            return
        
        # Schedule registration opening for each course in each group
        for group_id, group_info in queue_manager.groups.items():
            group_courses = group_info.get('courses', {})
            
            for course_id, course_info in group_courses.items():
                course_name = course_info.get('name', course_id)
                schedule = course_info.get('schedule', {})
                
                # Parse schedule
                schedule_day = schedule.get('day', 2)  # Default to Wednesday
                schedule_time_str = schedule.get('time', '20:00')  # Default to 20:00
                
                try:
                    time_parts = schedule_time_str.split(':')
                    schedule_time = time(int(time_parts[0]), int(time_parts[1]))
                except (ValueError, IndexError):
                    logger.warning(f"Invalid time format for {course_id} in group {group_id}, using default 20:00")
                    schedule_time = time(20, 0)
                
                # Calculate next occurrence of this day and time
                now = datetime.now(TIMEZONE)
                days_until_target = (schedule_day - now.weekday()) % 7
                if days_until_target == 0:  # Today
                    next_target_date = now.date()
                    next_reg_time = datetime.combine(next_target_date, schedule_time)
                    next_reg_time = TIMEZONE.localize(next_reg_time)
                    if next_reg_time <= now:  # Time has passed today
                        next_reg_time += timedelta(days=7)
                else:
                    next_target_date = now + timedelta(days=days_until_target)
                    next_reg_time = datetime.combine(next_target_date.date(), schedule_time)
                    next_reg_time = TIMEZONE.localize(next_reg_time)
                
                # Schedule the job for this specific course in this group
                job_queue.run_repeating(
                    lambda context, gid=group_id, cid=course_id: self.scheduled_open_course_registration(context, gid, cid),
                    interval=timedelta(weeks=1),
                    first=next_reg_time,
                    name=f'registration_opener_{course_id}'
                )
                
                day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                day_name = day_names[schedule_day]
                logger.info(f"Scheduled {course_name} registration opening: {day_name}s at {schedule_time_str} (next: {next_reg_time})")
    
    async def scheduled_open_course_registration(self, context: ContextTypes.DEFAULT_TYPE, group_id: str, course_id: str):
        """Scheduled task to open registration for a specific course in a group"""
        if group_id in queue_manager.groups and course_id in queue_manager.groups[group_id].get('courses', {}):
            queue_manager.open_course_registration(group_id, course_id)
            course_info = queue_manager.groups[group_id]['courses'][course_id]
            course_name = course_info.get('name', course_id)
            logger.info(f"Automatically opened registration for {course_name} in group {group_id}")
            await self.notify_course_registration_open(context, group_id, course_id)
    
    async def notify_course_registration_open(self, context: ContextTypes.DEFAULT_TYPE, group_id: str, course_id: str):
        """Send notification when registration opens for a specific course in a group"""
        if group_id not in queue_manager.groups or course_id not in queue_manager.groups[group_id].get('courses', {}):
            return
            
        course_info = queue_manager.groups[group_id]['courses'][course_id]
        course_name = course_info.get('name', course_id)
        message = f"🟢 **Registration Now Open!**\n\n"
        message += f"📚 Course: **{course_name}**\n"
        message += f"⏰ Opened at: {datetime.now(TIMEZONE).strftime('%H:%M')}\n\n"
        message += f"Use /register to join the queue!"
        
        # Send to all dev users and relevant group admins
        notification_users = set(queue_manager.dev_users)  # Dev users get all notifications
        
        # Add group admins for this specific group
        if str(group_id) in queue_manager.group_admins:
            notification_users.update(queue_manager.group_admins[str(group_id)])
        
        for admin_id in notification_users:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to send notification to admin {admin_id}: {e}")
        
        logger.info(f"Registration opened notification sent for {course_name}")
    
    # Legacy method for backward compatibility
    async def scheduled_open_registration(self, context: ContextTypes.DEFAULT_TYPE):
        """Legacy scheduled task - opens all courses"""
        queue_manager.open_registration()  # Opens all courses
        logger.info("Automatically opened registration for all courses (legacy)")
        await self.notify_registration_open(context)
    
    async def setup_bot_commands(self):
        """Set up default bot commands menu for all users (user commands only)"""
        user_commands = [
            BotCommand("start", "Запустить бота"),
            BotCommand("list", "Показать курсы"),
            BotCommand("register", "Записать"),
            BotCommand("unregister", "Отменить"),
            BotCommand("status", "Статус очереди"),
            BotCommand("myregistrations", "мои записи"),
            BotCommand("help", "Помощь"),
        ]
        
        # Set default commands (what regular users see)
        await self.application.bot.set_my_commands(
            user_commands, 
            scope=BotCommandScopeDefault()
        )
    
    async def setup_user_commands(self, user_id: int):
        """Set up personalized commands based on user permissions"""
        try:
            logger.info(f"Setting up commands for user {user_id}")
            logger.info(f"User {user_id} - is_dev: {self.is_dev_user(user_id)}, is_admin: {self.is_admin_user(user_id)}")
            
            user_commands = [
                BotCommand("start", "Запустить бота"),
                BotCommand("list", "Показать курсы"),
                BotCommand("register", "Записать"),
                BotCommand("unregister", "Отменить"),
                BotCommand("status", "Статус очереди"),
                BotCommand("myregistrations", "мои записи"),
                BotCommand("help", "Помощь"),
            ]
            
            # Check if user is admin or dev and add appropriate commands
            if self.is_dev_user(user_id):
                logger.info(f"User {user_id} is dev - setting up dev commands")
                # Dev users see all commands
                dev_commands = user_commands + [
                    BotCommand("admin_open", "Админ: Открыть регистрацию"),
                    BotCommand("admin_close", "Админ: Закрыть регистрацию"),
                    BotCommand("admin_clear", "Админ: Очистить очередь"),
                    BotCommand("admin_status", "Админ: Подробный статус"),
                    BotCommand("admin_add_course", "Админ: Добавить курс"),
                    BotCommand("admin_remove_course", "Админ: Удалить курс"),
                    BotCommand("admin_swap", "Админ: Поменять местами позиции"),
                    BotCommand("admin_config", "Админ: Показать конфигурацию"),
                    BotCommand("admin_queuesize", "Админ: Установить размер очереди"),
                    BotCommand("dev_clear_all", "Дев: Очистить все очереди"),
                    BotCommand("dev_queuesize", "Дев: Установить размер очереди (любая группа)"),
                    BotCommand("dev_add_admin", "Дев: Добавить админа"),
                    BotCommand("dev_list_admins", "Дев: Список админов"),
                    BotCommand("dev_remove_admin", "Дев: Удалить админа"),
                    BotCommand("dev_cleanup_groups", "Дев: Очистить группы"),
                    BotCommand("dev_blacklist_add", "Дев: Добавить в черный список"),
                    BotCommand("dev_blacklist_remove", "Дев: Удалить из черного списка"),
                    BotCommand("dev_blacklist_list", "Дев: Показать черный список"),
                ]
                await self.application.bot.set_my_commands(
                    dev_commands,
                    scope=BotCommandScopeChat(chat_id=user_id)
                )
                logger.info(f"Successfully set {len(dev_commands)} dev commands for user {user_id}")
            elif self.is_admin_user(user_id):
                logger.info(f"User {user_id} is admin - setting up admin commands")
                # Admin users see user + admin commands (but not dev commands)
                admin_commands = user_commands + [
                    BotCommand("admin_open", "Админ: Открыть регистрацию"),
                    BotCommand("admin_close", "Админ: Закрыть регистрацию"),
                    BotCommand("admin_clear", "Админ: Очистить очередь"),
                    BotCommand("admin_status", "Админ: Подробный статус"),
                    BotCommand("admin_add_course", "Админ: Добавить курс"),
                    BotCommand("admin_remove_course", "Админ: Удалить курс"),
                    BotCommand("admin_swap", "Админ: Поменять местами позиции"),
                    BotCommand("admin_config", "Админ: Показать конфигурацию"),
                    BotCommand("admin_queuesize", "Админ: Установить размер очереди"),
                ]
                await self.application.bot.set_my_commands(
                    admin_commands,
                    scope=BotCommandScopeChat(chat_id=user_id)
                )
                logger.info(f"Successfully set {len(admin_commands)} admin commands for user {user_id}")
            else:
                logger.info(f"User {user_id} is regular user - using default commands")
                # Regular users: Let them use default commands (no explicit setting)
                pass
                
        except Exception as e:
            logger.error(f"Failed to set up commands for user {user_id}: {e}")
    
    def is_admin_user(self, user_id: int) -> bool:
        """Check if user is admin (global admin or group admin)"""
        # Check if user is dev (has full access)
        if queue_manager.is_dev(user_id):
            return True
        
        # Check if user is global admin
        if queue_manager.is_admin(user_id):
            return True
        
        # Check if user is admin in any group
        for group_admins in queue_manager.group_admins.values():
            if user_id in group_admins:
                return True
        
        return False
    
    def is_dev_user(self, user_id: int) -> bool:
        """Check if user is dev"""
        return queue_manager.is_dev(user_id)
    
    def get_user_help_text(self, user_id: int, group_name: str) -> str:
        """Get personalized help text based on user permissions"""
        base_text = f"""🎓 **Университетский Бот запись на Курсы**
📍 Группа: {group_name}

Добро пожаловать! Этот бот поможет вам зарегистрироваться на университетские курсы.

**Команды:**
• `/list` - Показать все доступные курсы и расписания
• `/register` - Записать в очередь ✅ (только в ЛС)
• `/unregister` - Удалить ваши записи ✅ (только в ЛС)
• `/status` - Показать текущий статус очереди
• `/myregistrations` - Показать ваши записи ✅ (только в ЛС)
• `/help` - Показать это сообщение
• `/start` - Начать работу с ботом

**Как это работает:**
• Вы можете зарегистрировать нескольких человек (себя, друзей)
• Каждое имя может появиться только один раз в каждом курсе
• Регистрация открывается автоматически по расписанию
• 🔒 Регистрация происходит только в личных сообщениях"""

        # Add admin commands only for admin users
        if self.is_admin_user(user_id):
            admin_text = """

**Команды администратора:**
• `/admin_open` - Открыть регистрацию для курсов
• `/admin_close` - Закрыть регистрацию для курсов
• `/admin_clear` - Очистить очередь конкретного курса
• `/admin_status` - Подробный статус
• `/admin_config` - Показать конфигурацию
• `/admin_swap` - Поменять местами позиции в очереди
• `/admin_add_course` - Добавить новый курс
• `/admin_remove_course` - Удалить курс"""
            base_text += admin_text

        # Add dev-specific note
        if self.is_dev_user(user_id):
            dev_text = """

**🔧 Права разработчика:**
• Полный доступ ко всем группам и командам
• Возможность управления пользователями и конфигурацией системы"""
            base_text += dev_text

        base_text += "\n\nКаждый курс имеет свое расписание и открывается автоматически."
        return base_text
    
    async def post_init(self, application: Application) -> None:
        """Called after the bot has been initialized"""
        await self.setup_bot_commands()
        logger.info("Bot commands set up successfully")
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors that occur in the bot"""
        logger.error("Update '%s' caused error '%s'", update, context.error)
        
        # Handle network errors gracefully
        if "NetworkError" in str(context.error) or "RemoteProtocolError" in str(context.error):
            logger.warning("Network error occurred, bot will continue polling")
            return
        
        # Handle other specific errors if needed
        if update and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Sorry, an error occurred. Please try again later."
                )
            except Exception as e:
                logger.error("Failed to send error message: %s", e)
    
    def run(self):
        """Run the bot"""
        if not TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN not found in environment variables")
            return
        
        # Create application with better network settings
        self.application = (Application.builder()
                          .token(TELEGRAM_BOT_TOKEN)
                          .post_init(self.post_init)
                          .connect_timeout(30)
                          .read_timeout(30)
                          .write_timeout(30)
                          .pool_timeout(30)
                          .build())
        
        # Add error handler
        self.application.add_error_handler(self.error_handler)
        
        # Add group events handler (must be before command handlers)
        from telegram import ChatMemberUpdated
        self.application.add_handler(MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, 
            self.handle_new_chat_members
        ))
        
        # Add handlers (English commands only)
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("list", self.list_command))
        self.application.add_handler(CommandHandler("register", self.register_command))
        self.application.add_handler(CommandHandler("unregister", self.unregister_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("myregistrations", self.my_registrations_command))
        
        # Admin commands
        self.application.add_handler(CommandHandler("admin_open", self.admin_open_command))
        self.application.add_handler(CommandHandler("admin_close", self.admin_close_command))
        self.application.add_handler(CommandHandler("admin_clear", self.admin_clear_command))
        self.application.add_handler(CommandHandler("admin_status", self.admin_status_command))
        self.application.add_handler(CommandHandler("admin_config", self.admin_config_command))
        self.application.add_handler(CommandHandler("admin_queuesize", self.admin_queuesize_command))
        self.application.add_handler(CommandHandler("dev_queuesize", self.dev_queuesize_command))
        self.application.add_handler(CommandHandler("admin_swap", self.admin_swap_command))
        self.application.add_handler(CommandHandler("admin_add_course", self.admin_add_course_command))
        self.application.add_handler(CommandHandler("admin_remove_course", self.admin_remove_course_command))
        
        # Dev commands (global management)
        self.application.add_handler(CommandHandler("dev_add_admin", self.dev_add_admin_command))
        self.application.add_handler(CommandHandler("dev_list_admins", self.dev_list_admins_command))
        self.application.add_handler(CommandHandler("dev_remove_admin", self.dev_remove_admin_command))
        self.application.add_handler(CommandHandler("dev_cleanup_groups", self.dev_cleanup_groups_command))
        self.application.add_handler(CommandHandler("dev_test_group", self.dev_test_group_command))
        self.application.add_handler(CommandHandler("dev_clearQ_all", self.dev_clearQ_all_command))
        self.application.add_handler(CommandHandler("dev_blacklist_add", self.dev_blacklist_add_command))
        self.application.add_handler(CommandHandler("dev_blacklist_remove", self.dev_blacklist_remove_command))
        self.application.add_handler(CommandHandler("dev_blacklist_list", self.dev_blacklist_list_command))
        
        # Callback handler for inline keyboards
        self.application.add_handler(CallbackQueryHandler(self.callback_handler))
        
        # Message handler for name input
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            self.message_handler
        ))
        
        # Set up scheduler (if job queue is available)
        if self.application.job_queue is not None:
            self.setup_scheduler(self.application.job_queue)
        else:
            logger.warning("JobQueue not available. Scheduled registration opening disabled.")
            logger.info("Use /admin_open to manually open registration.")
        
        # Start the bot
        logger.info("Starting bot...")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
    
    async def handle_switch_group_callback(self, query, context):
        """Handle switch group callback - show available groups"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        user_id = query.from_user.id
        current_group_id = queue_manager.get_user_group(user_id)
        
        available_groups = queue_manager.groups
        
        if len(available_groups) <= 1:
            await query.edit_message_text(
                "❌ Нет других доступных групп для переключения."
            )
            return
        
        keyboard = []
        for group_id, group_info in available_groups.items():
            if group_id == current_group_id:
                continue  # Skip current group
                
            group_name = group_info.get('name', f'Group {group_id}')
            course_count = len(queue_manager.get_group_courses(group_id))
            
            keyboard.append([
                InlineKeyboardButton(
                    f"📚 {group_name} ({course_count} курсов)",
                    callback_data=f"confirm_switch_{group_id}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_switch")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🔄 **Выберите новую группу:**\n\n"
            "⚠️ *После смены группы ваш контекст изменится на выбранную группу.*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def handle_confirm_switch_callback(self, query, new_group_id, context):
        """Handle confirmed group switch with data reload"""
        user_id = query.from_user.id
        old_group_id = queue_manager.get_user_group(user_id)
        
        # Switch user to new group
        queue_manager.associate_user_with_group(user_id, new_group_id)
        
        # IMPORTANT: Reload queue data from file to get fresh state
        queue_manager.load_data()
        
        # Get new group info
        new_group_info = queue_manager.groups.get(new_group_id, {})
        new_group_name = new_group_info.get('name', f'Group {new_group_id}')
        course_count = len(queue_manager.get_group_courses(new_group_id))
        
        old_group_info = queue_manager.groups.get(old_group_id, {})
        old_group_name = old_group_info.get('name', f'Group {old_group_id}')
        
        await query.edit_message_text(
            f"✅ **Группа успешно изменена!**\n\n"
            f"📤 **Была:** {old_group_name}\n"
            f"📥 **Стала:** {new_group_name}\n"
            f"📊 **Доступно курсов:** {course_count}\n\n"
            f"Теперь используйте /register для записи на курсы в новой группе!",
            parse_mode='Markdown'
        )
        
        logger.info(f"User {user_id} switched from group {old_group_id} to {new_group_id}")
    
    async def handle_view_courses_callback(self, query, group_id, context):
        """Handle view courses callback - show courses directly"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        group_courses = queue_manager.get_group_courses(group_id)
        if not group_courses:
            await query.edit_message_text("� В этой группе пока нет доступных курсов.")
            return
        
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        message_text = f"📚 **Курсы в группе: {group_name}**\n\n"
        
        days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        group_schedules = queue_manager.get_group_schedules(group_id)
        
        for course_id, course_name in group_courses.items():
            # Get schedule info
            schedule_info = group_schedules.get(course_id, {"day": 2, "time": "20:00"})
            day_name = days[schedule_info['day']]
            time_str = schedule_info['time']
            
            # Get registration status
            is_open = queue_manager.is_course_registration_open(group_id, course_id)
            status_icon = "🟢" if is_open else "🔴"
            status_text = "Открыто" if is_open else "Закрыто"
            
            # Get queue count
            queue_count = len(queue_manager.group_queues[group_id].get(course_id, []))
            
            message_text += f"{status_icon} **{course_name}**\n"
            message_text += f"   📅 {day_name} в {time_str}\n"
            message_text += f"   📊 Статус очереди: {status_text}\n"
            message_text += f"   👥 Записано: {queue_count}\n\n"
        
        message_text += "📝 Нажмите \"Записаться на курсы\" для регистрации!"
        
        # Add back button to return to main menu
        keyboard = [
            [InlineKeyboardButton("🔙 Назад к меню", callback_data=f"back_to_menu_{group_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def handle_register_courses_callback(self, query, group_id, context):
        """Handle register courses callback"""
        await query.edit_message_text("📝 Открываю регистрацию... Используйте /register для записи на курсы.")
        
        # Trigger the register command functionality
        # We could redirect to existing register logic here
    
    async def handle_help_callback(self, query, group_id, context):
        """Handle help callback"""
        user_id = query.from_user.id
        group_info = queue_manager.groups.get(group_id, {})
        group_name = group_info.get('name', f'Group {group_id}')
        
        # Get personalized help text
        help_text = self.get_user_help_text(user_id, group_name)
        
        await query.edit_message_text(help_text, parse_mode='Markdown')
    
    async def handle_back_to_menu_callback(self, query, group_id, context):
        """Handle back to menu callback - return to main group menu"""
        await self.show_current_group_menu_edit(query, group_id)
    
    async def show_current_group_menu_edit(self, query, current_group_id: int):
        """Show current group status with management options (for editing existing messages)"""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        user_id = query.from_user.id
        
        # Get current group info
        group_info = queue_manager.groups.get(current_group_id, {})
        group_name = group_info.get('name', f'Group {current_group_id}')
        
        # Get course count for this group
        group_courses = queue_manager.get_group_courses(current_group_id)
        course_count = len(group_courses)
        
        # Check if user has admin permissions for this group
        is_admin = queue_manager.has_admin_access(user_id, current_group_id)
        admin_status = "👑 Администратор" if is_admin else "👤 Участник"
        
        # Create menu buttons
        keyboard = [
            [InlineKeyboardButton("📋 Просмотреть курсы", callback_data=f"view_courses_{current_group_id}")],
            [InlineKeyboardButton("📝 Записаться на курсы", callback_data=f"register_courses_{current_group_id}")],
            [InlineKeyboardButton("🔄 Сменить группу", callback_data="switch_group")],
            [InlineKeyboardButton("❓ Помощь", callback_data=f"help_{current_group_id}")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            f"👤 **Ваша текущая группа:**\n\n"
            f"📚 **Группа:** {group_name}\n"
            f"📊 **Курсов доступно:** {course_count}\n"
            f"🔰 **Статус:** {admin_status}\n\n"
            f"**Выберите действие:**"
        )
        
        await query.edit_message_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

if __name__ == '__main__':
    bot = UniversityRegistrationBot()
    bot.run()