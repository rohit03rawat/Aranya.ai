import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configure Google Gemini API
api_key = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=api_key)

# Initialize Gemini model
model = genai.GenerativeModel('gemini-2.0-flash')

# Store conversation history
conversations = {}

def create_system_prompt():
    """Create the system prompt for the therapy chatbot"""
    return """You are a supportive mental health chatbot designed to provide emotional support, 
    coping strategies, and general guidance. You are NOT a licensed therapist or medical professional.
    
    Guidelines to follow:
    1. Use a warm, empathetic tone and practice active listening
    2. Ask open-ended questions to help users explore their feelings
    3. Offer evidence-based coping strategies when appropriate
    4. NEVER diagnose conditions or suggest treatments/medications
    5. If a user expresses thoughts of self-harm or harm to others, provide crisis resources
    6. Respect privacy and maintain a non-judgmental approach
    7. Encourage professional help when appropriate
    
    Crisis resources to share when needed:
    - National Suicide Prevention Lifeline: 988 or 1-800-273-8255
    - Crisis Text Line: Text HOME to 741741
    """

def check_for_crisis(message):
    """
    Check if the message contains crisis indicators
    Returns True if crisis indicators detected
    """
    crisis_keywords = [
        "suicide", "kill myself", "want to die", "end my life", 
        "harm myself", "self-harm", "cutting myself", "hurt myself"
    ]
    
    return any(keyword in message.lower() for keyword in crisis_keywords)

def get_crisis_response():
    """Return a crisis response with resources"""
    return """I notice you mentioned something concerning. If you're having thoughts of harming yourself, 
    please reach out for immediate help:
    
    - National Suicide Prevention Lifeline: 988 or 1-800-273-8255 (available 24/7)
    - Crisis Text Line: Text HOME to 741741 (available 24/7)
    - Or go to your nearest emergency room
    
    These trained professionals can provide the support you need right now. Your life matters."""

def generate_response(user_id, message):
    """Generate a response using the Gemini API with simplified approach"""
    
    # Check for crisis indicators first
    if check_for_crisis(message):
        return get_crisis_response()
    
    # Get conversation history or create new
    if user_id not in conversations:
        conversations[user_id] = []
    
    conversation_history = conversations[user_id]
    
    # Create a prompt that includes system instructions and recent context
    system_instructions = create_system_prompt()
    
    # Build a context string with recent conversation history
    context = ""
    for entry in conversation_history[-3:]:  # Use last 3 messages for context
        context += f"User: {entry['user_message']}\n"
        if "bot_response" in entry:
            context += f"Assistant: {entry['bot_response']}\n"
    
    # Construct the final prompt
    final_prompt = f"{system_instructions}\n\nConversation History:\n{context}\n\nUser: {message}\nAssistant:"
    
    try:
        print("Sending simplified request to Gemini API...")
        response = model.generate_content(final_prompt)
        bot_response = response.text
        print(f"Received response: {bot_response[:50]}...")  # Print first 50 chars
        
        # Store in conversation history
        conversation_history.append({
            "timestamp": datetime.now().isoformat(),
            "user_message": message,
            "bot_response": bot_response
        })
        
        return bot_response
    
    except Exception as e:
        error_msg = f"Error generating response: {str(e)}"
        print(error_msg)
        return f"I'm having trouble responding right now. Technical details: {str(e)}"

@app.route('/')
def home():
    """Render the home page"""
    return render_template('index.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    """API endpoint for chat"""
    data = request.json
    user_id = data.get('user_id', 'default_user')
    message = data.get('message', '')
    
    response = generate_response(user_id, message)
    
    return jsonify({"response": response})
    
def test_gemini_api():
    try:
        # Use the existing model instance
        response = model.generate_content("Hello, can you give me a short response to test if you're working?")
        print("API Test successful! Received:", response.text)
        return True
    except Exception as e:
        print(f"API Test failed with error: {str(e)}")
        return False

if __name__ == '__main__':
    print("Testing Gemini API connection...")
    api_working = test_gemini_api()
    if api_working:
        print("API test successful, starting Flask app...")
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, debug=True)

    else:
        print("API test failed. Please check your API key and internet connection.")
