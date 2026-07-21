# components/chat.py
import streamlit as st
import re
import time
import io
from gtts import gTTS
from google.genai import types
from config import FACILITATOR_INSTRUCTION, ASSESSMENT_INSTRUCTION
from database import search_documents, log_study_interaction
from ai_services import get_embedding
from streamlit_mic_recorder import speech_to_text

# --- Helper to generate Text-to-Speech audio ---
def get_tts_audio(text, lang_code):
    try:
        # Clean text: remove URLs so the bot doesn't read them out loud
        clean_text = re.sub(r'https?://\S+', '', text)
        # Clean text: remove markdown symbols
        clean_text = clean_text.replace('*', '').replace('#', '').replace('_', '')
        
        if not clean_text.strip():
            return None

        # Map app language codes to gTTS language codes
        gtts_lang = 'en'
        if 'yue' in lang_code:
            gtts_lang = 'yue' # Cantonese
        elif 'zh' in lang_code:
            gtts_lang = 'zh-CN' # Mandarin
        elif 'es' in lang_code:
            gtts_lang = 'es' # Spanish
            
        tts = gTTS(text=clean_text, lang=gtts_lang)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        return fp.getvalue()
    except Exception as e:
        print(f"TTS Error: {e}")
        return None

# --- Callback to handle form submission safely ---
def handle_submit():
    if st.session_state.text_input_box:
        # Save the prompt to be processed
        st.session_state.submitted_prompt = st.session_state.text_input_box
        # Clear the text box state BEFORE it gets rendered again
        st.session_state.text_input_box = ""
        st.session_state.last_voice_prompt = ""

def render_chat(supabase, gemini_client, chat_model_name, embedding_model_name):
    # --- Custom CSS for the Text Input Border and Form ---
    st.markdown("""
        <style>
        /* Remove border and padding from the form to make it blend in */
        [data-testid="stForm"] {
            border: none !important;
            padding: 0 !important;
        }
        /* Target the Streamlit text input box to add a custom border */
        div[data-testid="stTextInput"] div[data-baseweb="input"] {
            border: 2px solid #4A90E2 !important; 
            border-radius: 8px !important; 
            transition: border-color 0.3s ease-in-out;
        }
        /* Make the border change color slightly when clicked/focused */
        div[data-testid="stTextInput"] div[data-baseweb="input"]:focus-within {
            border: 2px solid #2C3E50 !important; 
        }
        </style>
    """, unsafe_allow_html=True)

    # Initialize state variables
    if "submitted_prompt" not in st.session_state:
        st.session_state.submitted_prompt = ""
    if "last_voice_prompt" not in st.session_state:
        st.session_state.last_voice_prompt = ""
    if "text_input_box" not in st.session_state:
        st.session_state.text_input_box = ""
    if "current_url" not in st.session_state:
        st.session_state.current_url = None
    if "tts_enabled" not in st.session_state:
        st.session_state.tts_enabled = True

    # --- 1. SIDEBAR: Voice Input & Webpage Viewer ---
    with st.sidebar:
        supported_languages = {
            "Cantonese (廣東話)": "yue-Hant-HK",
            "English": "en",
            "Mandarin (普通話)": "zh-CN",
            "Spanish": "es-ES"
        }
        
        st.markdown("### ⚙️ Voice Input Settings")
        selected_lang_name = st.selectbox(
            "Select your preferred language:",
            list(supported_languages.keys()),
            index=0
        )
        selected_lang_code = supported_languages[selected_lang_name]

        st.markdown("### 🔊 Audio Settings")
        # Toggle for Text-to-Speech
        st.toggle("Enable Text-to-Speech (TTS)", value=st.session_state.tts_enabled, key="tts_enabled")

        st.markdown("---")
        
        # Webpage Viewer in Sidebar
        st.subheader("🖥️ Webpage Viewer")
        if st.session_state.current_url:
            # Render the webpage in an iframe inside the sidebar
            st.iframe(st.session_state.current_url, height=500)
            if st.button("Clear Viewer"):
                st.session_state.current_url = None
                st.rerun()
        else:
            # Placeholder when no link has been shared yet
            st.info("Visuals and videos will appear here when you click an 'Open in Sidebar' button!")
    
    # --- HEADER ---
    if st.session_state.active_bot == "facilitator":
        st.subheader("👨‍🏫 I am Einstein Junior, your science teacher!")
    else:
        st.subheader("📝 I am the Assessment Bot!")

    # ==========================================
    # CHAT INTERFACE (Main Area)
    # ==========================================
    # --- 2. Scrollable Chat Container ---
    chat_container = st.container(height=600)
    
    with chat_container:
        for i, message in enumerate(st.session_state.messages):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                
                # Render Audio Player if audio exists AND TTS is enabled
                if message.get("audio") and st.session_state.tts_enabled:
                    # Only autoplay the very last message in the chat history
                    is_latest_message = (i == len(st.session_state.messages) - 1)
                    st.audio(message["audio"], format="audio/mp3", autoplay=is_latest_message)
                
                # If the assistant posted a link, provide a button to open it in the sidebar
                if message["role"] == "assistant":
                    urls = re.findall(r'(https?://[^\s\)]+)', message["content"])
                    # Deduplicate URLs
                    urls = list(dict.fromkeys(urls))
                    if urls:
                        for j, raw_url in enumerate(urls):
                            if st.button(f"📺 Open in Sidebar: {raw_url[:30]}...", key=f"btn_open_sidebar_{i}_{j}"):
                                embed_url = raw_url
                                # Convert YouTube Shorts to embed format
                                if "youtube.com/shorts/" in raw_url:
                                    embed_url = raw_url.replace("youtube.com/shorts/", "youtube.com/embed/")
                                # Convert Google Drive view links to preview format
                                elif "drive.google.com/file/d/" in raw_url:
                                    embed_url = raw_url.replace("/view?usp=sharing", "/preview").replace("/view", "/preview")
                                
                                st.session_state.current_url = embed_url
                                st.rerun()

    # --- 3. Custom Input Row (Locked below the chat) ---
    input_container = st.container()
    
    with input_container:
        col1, col2 = st.columns([5, 1])
        
        # Process Microphone first so it can update the text box state safely
        with col2:
            voice_prompt = speech_to_text(
                language=selected_lang_code,
                start_prompt="🎙️ Speak",
                stop_prompt="🛑 Stop",
                just_once=True,
                key=f'voice_input_{selected_lang_code}'
            )
            
            # If a new voice transcription is received, populate the text box state
            if voice_prompt and voice_prompt != st.session_state.last_voice_prompt:
                st.session_state.text_input_box = voice_prompt
                st.session_state.last_voice_prompt = voice_prompt
                st.rerun()

        # Use a form for the text input to guarantee 'Enter' submits the value
        with col1:
            with st.form("chat_form", clear_on_submit=False):
                form_col1, form_col2 = st.columns([5, 1])
                with form_col1:
                    st.text_input(
                        "Type your message...", 
                        key="text_input_box", 
                        label_visibility="collapsed"
                    )
                with form_col2:
                    # Attach the callback here! This runs before the script reruns.
                    st.form_submit_button("Send", on_click=handle_submit)

    # --- 4. Determine the prompt ---
    prompt = None
    if st.session_state.submitted_prompt:
        prompt = st.session_state.submitted_prompt
        st.session_state.submitted_prompt = "" # Reset it after capturing

    # --- 5. Process the prompt ---
    if prompt:
        with chat_container:
            with st.chat_message("user"):
                st.markdown(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})

        with chat_container:
            with st.spinner("Thinking..."):
                query_embedding = get_embedding(gemini_client, prompt, embedding_model_name)
                retrieved_chunks = search_documents(supabase, query_embedding) if query_embedding else []
                
                if retrieved_chunks:
                    context_texts = [chunk['content'] for chunk in retrieved_chunks]
                    current_rag_context = "\n\n---\n\n".join(context_texts)
                else:
                    current_rag_context = "No specific context found in the Knowledge Base."

        gemini_history = [
            types.Content(role="user" if msg["role"] == "user" else "model", parts=[types.Part.from_text(text=msg["content"])])
            for msg in st.session_state.messages[:-1]
        ]

        current_instruction = FACILITATOR_INSTRUCTION if st.session_state.active_bot == "facilitator" else ASSESSMENT_INSTRUCTION
        bot_name_context = "Einstein Junior (Facilitator)" if st.session_state.active_bot == "facilitator" else "Assessment Bot"

        # Instruct Gemini to match the language
        augmented_prompt = (
            f"You are {bot_name_context}. Use the following Knowledge Base to inform your response. "
            f"IMPORTANT: You must respond in the exact same language that the student used in their query.\n\n"
            f"Knowledge Base Context:\n{current_rag_context}\n\n"
            f"Student's Query:\n{prompt}"
        )

        with chat_container:
            with st.chat_message("assistant"):
                message_placeholder = st.empty()
                
                max_retries = 3
                retry_delay = 2
                
                for attempt in range(max_retries):
                    try:
                        chat_session = gemini_client.chats.create(
                            model=chat_model_name,
                            config=types.GenerateContentConfig(system_instruction=current_instruction),
                            history=gemini_history
                        )
                        
                        response_stream = chat_session.send_message_stream(augmented_prompt)
                        full_response = ""
                        for chunk in response_stream:
                            if chunk.text:
                                full_response += chunk.text
                                display_text = full_response.replace("[HANDOFF]", "").replace("[HANDBACK]", "")
                                message_placeholder.markdown(display_text + "▌")
                        
                        # Log to database BEFORE any st.rerun() happens
                        log_study_interaction(supabase, st.session_state.user.id, prompt, full_response)
                        
                        if "[HANDOFF]" in full_response:
                            clean_response = full_response.replace("[HANDOFF]", "").strip()
                            message_placeholder.markdown(clean_response)
                            
                            # Only generate audio if TTS is enabled
                            audio_bytes = get_tts_audio(clean_response, selected_lang_code) if st.session_state.tts_enabled else None
                            st.session_state.messages.append({"role": "assistant", "content": clean_response, "audio": audio_bytes})
                            
                            st.session_state.active_bot = "assessment"
                            handoff_greeting = "Hello! I am the Assessment Bot. Einstein Junior tells me you have a great understanding of aerodynamics! Would you like to take a 5-question quiz to test your knowledge?"
                            
                            greeting_audio = get_tts_audio(handoff_greeting, selected_lang_code) if st.session_state.tts_enabled else None
                            st.session_state.messages.append({"role": "assistant", "content": handoff_greeting, "audio": greeting_audio})
                            st.rerun()
                            
                        elif "[HANDBACK]" in full_response:
                            clean_response = full_response.replace("[HANDBACK]", "").strip()
                            message_placeholder.markdown(clean_response)
                            
                            # Only generate audio if TTS is enabled
                            audio_bytes = get_tts_audio(clean_response, selected_lang_code) if st.session_state.tts_enabled else None
                            st.session_state.messages.append({"role": "assistant", "content": clean_response, "audio": audio_bytes})
                            
                            st.session_state.active_bot = "facilitator"
                            handback_greeting = "Hello again! I am Einstein Junior. I heard you just finished your assessment. How did you do? Are you ready to learn more about science?"
                            
                            greeting_audio = get_tts_audio(handback_greeting, selected_lang_code) if st.session_state.tts_enabled else None
                            st.session_state.messages.append({"role": "assistant", "content": handback_greeting, "audio": greeting_audio})
                            st.rerun()
                            
                        else:
                            message_placeholder.markdown(full_response)
                            
                            # Only generate audio if TTS is enabled
                            audio_bytes = get_tts_audio(full_response, selected_lang_code) if st.session_state.tts_enabled else None
                            st.session_state.messages.append({"role": "assistant", "content": full_response, "audio": audio_bytes})
                            
                            # Trigger a rerun so any new links generate their "Open in Sidebar" buttons immediately
                            # It also triggers the audio player to render and autoplay
                            st.rerun()
                                
                        break # Break out of the retry loop if successful
                        
                    except Exception as e:
                        error_msg = str(e)
                        if "503" in error_msg or "UNAVAILABLE" in error_msg:
                            if attempt < max_retries - 1:
                                message_placeholder.warning(f"Google's servers are busy. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{max_retries})")
                                time.sleep(retry_delay)
                                continue
                            else:
                                message_placeholder.error("Google's servers are currently experiencing high demand. Please wait a moment and try sending your message again.")
                        else:
                            message_placeholder.error(f"Error communicating with Gemini: {e}")
                            break
