import os
from mistralai import Mistral
import discord
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
from typing import Optional
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
from timezonefinder import TimezoneFinder
import pytz

MISTRAL_MODEL = "mistral-large-latest"
SYSTEM_PROMPT = """You are a helpful assistant with access to my calendar and location information.
When responding to location queries, return the exact formatted location details provided without reformatting.
For other queries, provide helpful and concise responses."""
SCOPES = [
    'https://www.googleapis.com/auth/calendar',  # Full access
    'https://www.googleapis.com/auth/calendar.events'  # Specific for events
]

class MistralAgent:
    def __init__(self):
        MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
        self.location = os.getenv("USER_LOCATION")
        if not self.location:
            self.location = self.get_ip_location()
        self.client = Mistral(api_key=MISTRAL_API_KEY)
        self.calendar_service = self.setup_calendar()
        self.maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        # Add conversation memory
        self.conversation_history = {}  # Store conversation by user ID
        self.memory_limit = 10  # Keep last 10 messages
        self.tf = TimezoneFinder()
        self.update_location_and_timezone()

    def get_ip_location(self) -> str:
        """Get location from IP address using multiple services for reliability"""
        try:
            # Try ip-api.com first (more accurate)
            response = requests.get('http://ip-api.com/json/', timeout=5)
            data = response.json()
            if data['status'] == 'success':
                return f"{data['city']}, {data['regionName']}, {data['country']}"

            # Fallback to ipapi.co
            response = requests.get('https://ipapi.co/json/', timeout=5)
            data = response.json()
            if response.status_code == 200:
                return f"{data['city']}, {data['region']}, {data['country']}"

            # Try another fallback: ipinfo.io
            response = requests.get('https://ipinfo.io/json', timeout=5)
            data = response.json()
            if 'city' in data and 'region' in data:
                return f"{data['city']}, {data['region']}, {data['country']}"

        except Exception as e:
            print(f"Location detection error: {str(e)}")
            
        # If all services fail, try to get location from environment variable
        env_location = os.getenv("USER_LOCATION")
        if env_location:
            return env_location
            
        return "Location unknown"  # Last resort fallback

    def setup_calendar(self):
        creds = None
        # The file token.pickle stores the user's access and refresh tokens
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
                
        # If there are no (valid) credentials available, let the user log in
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)

        return build('calendar', 'v3', credentials=creds)

    async def get_upcoming_events(self, max_results=10):
        """Get upcoming events in local timezone"""
        now = self.get_local_time()
        events_result = self.calendar_service.events().list(
            calendarId='primary',
            timeMin=now.astimezone(timezone.utc).isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        return events_result.get('items', [])

    async def get_weather(self) -> Optional[str]:
        """Get current weather for user's location"""
        api_key = os.getenv("WEATHER_API_KEY")
        if not api_key:
            return None
            
        try:
            # Extract just the city name from location string
            city = self.location.split(',')[0].strip()
            
            url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric"
            response = requests.get(url)
            data = response.json()
            
            if response.status_code == 200:
                temp_c = data['main']['temp']
                temp_f = (temp_c * 9/5) + 32  # Convert to Fahrenheit
                return f"Current weather in {city}: {data['weather'][0]['description']}, {temp_c:.1f}°C ({temp_f:.1f}°F)"
            else:
                print(f"Weather API error: {data.get('message', 'Unknown error')}")
                return None
                
        except Exception as e:
            print(f"Error getting weather: {str(e)}")
            return None

    async def get_travel_time(self, destination: str) -> Optional[str]:
        """Get travel times to destination"""
        if not self.maps_api_key:
            return None
            
        try:
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                'origins': self.location,
                'destinations': destination,
                'mode': 'driving',
                'key': self.maps_api_key
            }
            driving = requests.get(url, params=params).json()
            
            params['mode'] = 'walking'
            walking = requests.get(url, params=params).json()
            
            drive_time = driving['rows'][0]['elements'][0]['duration']['text']
            walk_time = walking['rows'][0]['elements'][0]['duration']['text']
            distance = driving['rows'][0]['elements'][0]['distance']['text']
            
            return f"Distance: {distance}\nDriving time: {drive_time}\nWalking time: {walk_time}"
        except:
            return None

    async def get_next_event_travel_info(self) -> Optional[str]:
        """Get travel info for next event"""
        events = await self.get_upcoming_events(1)
        if not events:
            return "No upcoming events found."
            
        event = events[0]
        location = event.get('location')
        if not location:
            return "Next event has no location specified."
            
        start_time = event['start'].get('dateTime', event['start'].get('date'))
        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        local_time = start_dt.astimezone(self.timezone)
        
        travel_info = await self.get_travel_time(location)
        if not travel_info:
            return f"Could not calculate travel time to: {location}"
            
        return f"Next event: {event['summary']} at {local_time.strftime('%I:%M %p')}\nLocation: {location}\n{travel_info}"

    async def get_event_details(self, event) -> str:
        """Get detailed information about an event including attendees"""
        start_time = event['start'].get('dateTime', event['start'].get('date'))
        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        local_time = start_dt.astimezone(self.timezone)
        
        details = f"- {event['summary']} ({local_time.strftime('%I:%M %p %Z')})"
        
        if 'location' in event:
            details += f"\n  Location: {event['location']}"
            if self.maps_api_key:
                travel_info = await self.get_travel_time(event['location'])
                if travel_info:
                    details += f"\n  {travel_info}"
            
        if 'attendees' in event:
            details += "\n  Attendees:"
            for attendee in event['attendees']:
                status = attendee.get('responseStatus', 'no response')
                name = attendee.get('displayName', attendee['email'])
                details += f"\n    - {name} ({status})"
                
        return details

    async def create_event(self, summary: str, start_time: str, end_time: str, 
                          location: str = None, attendees: list = None) -> str:
        try:
            now = self.get_local_time()
            
            # Parse or create start time
            if isinstance(start_time, str) and not start_time.endswith('Z'):
                # Handle relative times like "tomorrow at noon"
                if "tomorrow" in start_time.lower():
                    start_dt = now + timedelta(days=1)
                    start_dt = start_dt.replace(hour=12, minute=0, second=0, microsecond=0)
                else:
                    start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                start_dt = start_dt.astimezone(self.timezone)
            else:
                start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                start_dt = start_dt.astimezone(self.timezone)

            # Set end time to 1 hour after start if not provided
            if not end_time:
                end_dt = start_dt + timedelta(hours=1)
            else:
                end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                end_dt = end_dt.astimezone(self.timezone)

            event = {
                'summary': summary,
                'start': {
                    'dateTime': start_dt.isoformat(),
                    'timeZone': str(self.timezone)
                },
                'end': {
                    'dateTime': end_dt.isoformat(),
                    'timeZone': str(self.timezone)
                }
            }
            
            if location:
                event['location'] = location

            print(f"Attempting to create event with data: {event}")

            # Create the event
            created_event = self.calendar_service.events().insert(
                calendarId='primary',
                body=event,
                sendUpdates='all'
            ).execute()
            
            print(f"Event creation response: {created_event}")
            
            # Verify the event exists
            verify_event = self.calendar_service.events().get(
                calendarId='primary',
                eventId=created_event['id']
            ).execute()
            
            if verify_event:
                return f"Event created successfully: {verify_event.get('htmlLink')}"
            else:
                return "Event creation failed verification"
            
        except Exception as e:
            print(f"Error creating event: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return f"Failed to create event: {str(e)}"

    def get_conversation_history(self, user_id: str) -> list:
        """Get conversation history for a user"""
        if user_id not in self.conversation_history:
            self.conversation_history[user_id] = []
        return self.conversation_history[user_id]

    def add_to_history(self, user_id: str, role: str, content: str):
        """Add a message to conversation history"""
        history = self.get_conversation_history(user_id)
        history.append({"role": role, "content": content})
        # Keep only the last N messages
        if len(history) > self.memory_limit:
            history.pop(0)
        self.conversation_history[user_id] = history

    async def get_location_details(self) -> str:
        """Get detailed location information"""
        try:
            response = requests.get('http://ip-api.com/json/', timeout=5)
            data = response.json()
            if data['status'] == 'success':
                return (f"📍 Location: {data['city']}, {data['regionName']}, {data['country']}\n"
                       f"🌐 Coordinates: {data['lat']:.4f}°N, {data['lon']:.4f}°W\n"
                       f"🕒 Timezone: {self.timezone}\n"
                       f"🏢 ISP: {data['isp']}")
        except Exception as e:
            print(f"Error getting location details: {str(e)}")
        return f"📍 Location: {self.location}"

    async def run(self, message: discord.Message):
        user_id = str(message.author.id)
        msg_lower = message.content.lower()

        # Direct responses without going through Mistral
        location_queries = [
            'what is my location', 
            'where am i', 
            'what\'s my location', 
            'where', 
            'location',
            'what is my current location',
            'tell me my location'
        ]
        
        if any(query == msg_lower for query in location_queries):  # Exact match only
            return await self.get_location_details()

        # Get existing conversation history
        history = self.get_conversation_history(user_id)

        if msg_lower == "forget" or msg_lower == "reset":
            self.conversation_history[user_id] = []
            return "I've reset our conversation history."

        # Check for event creation
        if any(phrase in msg_lower for phrase in 
               ['schedule a', 'create event', 'add meeting', 'new appointment']):
            try:
                messages = [
                    {"role": "system", "content": """RESPOND WITH RAW JSON ONLY. NO CODE BLOCKS. NO MARKDOWN.
                    Example:
                    {
                        "summary": "Event title here",
                        "start_time": "2025-02-21T12:00:00-05:00",
                        "location": "Location here"
                    }"""},
                    {"role": "user", "content": "Create this event: " + message.content}
                ]

                response = await self.client.chat.complete_async(
                    model=MISTRAL_MODEL,
                    messages=messages,
                )
                
                content = response.choices[0].message.content.strip()
                if content.startswith('```'):
                    content = content.split('\n', 1)[1]
                    content = content.rsplit('\n', 1)[0]
                
                event_info = json.loads(content)
                result = await self.create_event(
                    summary=event_info['summary'],
                    start_time=event_info['start_time'],
                    end_time=None,
                    location=event_info.get('location')
                )
                
                return f"✅ Event created!\n{result}"

            except Exception as e:
                print("DEBUG - Error:", str(e))
                return f"Failed to create event: {str(e)}"

        # Check for postpone requests
        if 'postpone' in msg_lower:
            try:
                # Extract event name and hours
                words = msg_lower.split()
                event_name = None
                hours = 1  # default to 1 hour

                for i, word in enumerate(words):
                    if word == 'postpone':
                        # Get event name (assuming it's the next word)
                        if i + 1 < len(words):
                            event_name = words[i + 1]
                    elif word in ['hour', 'hours']:
                        # Get number of hours (assuming it's the previous word)
                        if i > 0 and words[i-1].isdigit():
                            hours = int(words[i-1])

                if event_name:
                    return await self.postpone_event(event_name, hours)
                else:
                    return "Please specify which event to postpone."

            except Exception as e:
                print("DEBUG - Error:", str(e))
                return f"Failed to postpone event: {str(e)}"

        # Check for location update requests
        if 'update location' in msg_lower or 'change location' in msg_lower:
            try:
                # Extract event name and location more reliably
                if 'update location of' in msg_lower:
                    parts = msg_lower.split('update location of')[1]
                elif 'change location of' in msg_lower:
                    parts = msg_lower.split('change location of')[1]
                else:
                    return "Please use format: update location of [event name] to [new location]"

                if ' to ' in parts:
                    event_name, new_location = parts.split(' to ')
                elif ' at ' in parts:
                    event_name, new_location = parts.split(' at ')
                else:
                    return "Please specify the new location using 'to' or 'at'"

                event_name = event_name.strip()
                new_location = new_location.strip()

                if event_name and new_location:
                    return await self.update_event_location(event_name, new_location)
                else:
                    return "Please specify both the event name and new location."

            except Exception as e:
                print("DEBUG - Error:", str(e))
                return f"Failed to update event location: {str(e)}"

        # Check for attendee update requests
        if 'add attendee' in msg_lower or 'invite' in msg_lower:
            try:
                words = msg_lower.split()
                event_name = None
                attendees = []
                
                # Find the event name and attendees
                for i, word in enumerate(words):
                    if word in ['to', 'for'] and i > 0:
                        event_name = ' '.join(words[2:i])  # Words between "add attendee" and "to/for"
                        # Extract email addresses from the rest of the message
                        attendees = [word for word in words[i+1:] if '@' in word]
                        break

                if event_name and attendees:
                    return await self.update_event_attendees(event_name, attendees)
                else:
                    return "Please specify the event name and attendee email addresses."

            except Exception as e:
                print("DEBUG - Error:", str(e))
                return f"Failed to update event attendees: {str(e)}"

        # Build context
        location_context = f"My location: {self.location}\n"  # Simplified location for context
        
        weather = await self.get_weather()
        if weather:
            location_context += f"{weather}\n"

        # Add calendar context if needed
        calendar_context = ""
        if any(word in msg_lower for word in ['calendar', 'schedule', 'event', 'meeting']):
            events = await self.get_upcoming_events(5)
            calendar_context = "Here are my upcoming events:\n"
            for event in events:
                calendar_context += await self.get_event_details(event) + "\n"

        # Add travel context if needed
        if any(word in msg_lower for word in ['far', 'distance', 'travel time', 'how long']):
            travel_info = await self.get_next_event_travel_info()
            if travel_info:
                location_context += f"\n{travel_info}\n"

        # Build messages list with history
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ] + history + [
            {"role": "user", "content": f"{location_context}{calendar_context}\n{message.content}"}
        ]

        response = await self.client.chat.complete_async(
            model=MISTRAL_MODEL,
            messages=messages,
        )

        # Add the new exchange to history
        self.add_to_history(user_id, "user", message.content)
        self.add_to_history(user_id, "assistant", response.choices[0].message.content)

        return response.choices[0].message.content

    def update_location_and_timezone(self):
        """Update location and get local timezone"""
        try:
            # Get precise location using IP
            response = requests.get('http://ip-api.com/json/', timeout=5)
            data = response.json()
            if data['status'] == 'success':
                self.location = f"{data['city']}, {data['regionName']}, {data['country']}"
                self.latitude = data['lat']
                self.longitude = data['lon']
                
                # Get timezone from coordinates
                timezone_str = self.tf.timezone_at(lat=self.latitude, lng=self.longitude)
                self.timezone = pytz.timezone(timezone_str)
                print(f"Located in timezone: {timezone_str}")
            else:
                raise Exception("Location service failed")
        except Exception as e:
            print(f"Error updating location: {str(e)}")
            # Fallback to system timezone
            self.timezone = pytz.timezone('America/Los_Angeles')  # Default to Pacific Time
            
    def get_local_time(self) -> datetime:
        """Get current time in local timezone"""
        return datetime.now(self.timezone)

    async def modify_event(self, event_id: str, changes: dict) -> str:
        """Modify an existing calendar event"""
        try:
            # Get the existing event
            event = self.calendar_service.events().get(
                calendarId='primary',
                eventId=event_id
            ).execute()

            # Create a copy of the existing event
            updated_event = event.copy()
            
            # Apply the changes while preserving existing fields
            for key, value in changes.items():
                if isinstance(value, dict):
                    if key not in updated_event:
                        updated_event[key] = {}
                    updated_event[key].update(value)
                else:
                    updated_event[key] = value

            # Update the event
            result = self.calendar_service.events().update(
                calendarId='primary',
                eventId=event_id,
                body=updated_event,
                sendUpdates='all'
            ).execute()

            print(f"Event update response: {result}")  # Debug print
            return f"Event updated successfully: {result.get('htmlLink')}"
            
        except Exception as e:
            print(f"Error modifying event: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return f"Failed to modify event: {str(e)}"

    async def postpone_event(self, event_summary: str, hours: int) -> str:
        """Postpone an event by specified hours"""
        try:
            # Find the event
            events = await self.get_upcoming_events(10)
            target_event = None
            
            for event in events:
                if event['summary'].lower() == event_summary.lower():
                    target_event = event
                    break
            
            if not target_event:
                return f"Could not find event '{event_summary}'"

            # Get current start and end times
            start_time = datetime.fromisoformat(
                target_event['start']['dateTime'].replace('Z', '+00:00')
            )
            end_time = datetime.fromisoformat(
                target_event['end']['dateTime'].replace('Z', '+00:00')
            )

            # Add hours
            new_start = start_time + timedelta(hours=hours)
            new_end = end_time + timedelta(hours=hours)

            # Prepare changes
            changes = {
                'start': {
                    'dateTime': new_start.isoformat(),
                    'timeZone': str(self.timezone)
                },
                'end': {
                    'dateTime': new_end.isoformat(),
                    'timeZone': str(self.timezone)
                }
            }

            # Update the event
            result = await self.modify_event(target_event['id'], changes)
            return f"Event '{event_summary}' postponed by {hours} hours.\n{result}"

        except Exception as e:
            print(f"Error postponing event: {str(e)}")
            return f"Failed to postpone event: {str(e)}"

    async def update_event_location(self, event_summary: str, new_location: str) -> str:
        """Update an event's location"""
        try:
            # Find the event
            events = await self.get_upcoming_events(10)
            target_event = None
            
            for event in events:
                if event['summary'].lower() == event_summary.lower():
                    target_event = event
                    break
            
            if not target_event:
                return f"Could not find event '{event_summary}'"

            # Prepare changes
            changes = {
                'location': new_location
            }

            # Update the event
            result = await self.modify_event(target_event['id'], changes)
            return f"Updated location for '{event_summary}' to: {new_location}\n{result}"

        except Exception as e:
            print(f"Error updating event location: {str(e)}")
            return f"Failed to update event location: {str(e)}"

    async def update_event_attendees(self, event_summary: str, attendees: list) -> str:
        """Add or update event attendees"""
        try:
            # Find the event
            events = await self.get_upcoming_events(10)
            target_event = None
            
            for event in events:
                if event['summary'].lower() == event_summary.lower():
                    target_event = event
                    break
            
            if not target_event:
                return f"Could not find event '{event_summary}'"

            # Get existing attendees if any
            existing_attendees = target_event.get('attendees', [])
            existing_emails = {att['email'] for att in existing_attendees}

            # Add new attendees
            new_attendees = [{'email': email} for email in attendees if email not in existing_emails]
            all_attendees = existing_attendees + new_attendees

            # Prepare changes
            changes = {
                'attendees': all_attendees
            }

            # Update the event
            result = await self.modify_event(target_event['id'], changes)
            return f"Updated attendees for '{event_summary}'\nNew attendees: {', '.join(attendees)}\n{result}"

        except Exception as e:
            print(f"Error updating event attendees: {str(e)}")
            return f"Failed to update event attendees: {str(e)}"
