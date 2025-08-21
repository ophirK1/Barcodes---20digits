import os
import shutil
import socket
import time
import multiprocessing
import subprocess
import usb.core
from gpiozero import LED, Button
import socketserver

# ==============================================================================
# --- CONFIGURATION ---
# ==============================================================================

# --- Network Settings ---
SERVER_IP = "192.168.0.60"
SERVER_PORT = 3333

# --- Hardware Pins ---
GATE_LED_PIN = 26
CONFIG_BUTTON_PIN = 27

# --- File Paths ---
BARCODE_DIR = "/home/admin/Barcodes"
# CORRECTED: Path now supports multiple sounds
SOUND_PATH = "/home/admin/Barcodes/sounds/{}.mp3" 
# ADDED: List of files/folders to protect during a database wipe
FILES_TO_KEEP = ["sounds", "main.py", "install.sh", "requirements.txt", "install guide barcode.txt"]

# --- Barcode Rules (20-Digit Format) ---
VALID_SPECIAL_KEYS = [""]
MASTER_KEY_PREFIX = "123456780"
MASTER_KEY_SUFFIX = "12345678"

# --- USB Scanner (Client) ---
USB_VENDORS = [(0x1eab, 0x1a03), (0x27dd, 0x0103)]
PING_TIMEOUT = 0.2


# ==============================================================================
# --- SHARED VALIDATION LOGIC (20-Digit Format) ---
# ==============================================================================

def create_barcode_file(site_id, special_key, registry_id, numerator):
    """Creates the directory structure and the final barcode file for the 20-digit format."""
    try:
        path = os.path.join(BARCODE_DIR, site_id, special_key, registry_id)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, f"{numerator}.txt"), "w") as f:
            pass
        print(f"Successfully created file for barcode.")
        return True
    except OSError as e:
        print(f"Error creating file: {e}")
        return False

# MODIFIED: Now accepts a simple boolean for the button state
def process_barcode_locally(barcode, button_is_pressed):
    """Validates a 20-digit barcode against the local filesystem."""
    if not isinstance(barcode, str) or len(barcode) != 20 or not barcode.isdigit():
        print(f"LOCAL VALIDATION FAILED: Barcode must be 20 digits.")
        return False

    special_key = barcode[0:5]
    numerator = barcode[5:15]
    registry_id = barcode[15:17]
    site_id = barcode[17:20]

    print(f"LOCAL VALIDATION: Key={special_key}, Site={site_id}, Registry={registry_id}, Numerator={numerator}")

    if barcode.startswith(MASTER_KEY_PREFIX) and barcode.endswith(MASTER_KEY_SUFFIX):
        site_id_from_master = barcode[len(MASTER_KEY_PREFIX):-len(MASTER_KEY_SUFFIX)]
        site_path = os.path.join(BARCODE_DIR, site_id_from_master)
        if os.path.isdir(site_path):
            print(f"LOCAL ACCESS GRANTED: Master key used for existing site '{site_id_from_master}'.")
            return True
        else:
            print(f"LOCAL VALIDATION FAILED: Master key used for non-existent site '{site_id_from_master}'.")
            return False

    # MODIFIED: Check uses the boolean flag directly
    if button_is_pressed:
        print("LOCAL ACCESS GRANTED: Manual override.")
        site_path = os.path.join(BARCODE_DIR, site_id)
        barcode_file_path = os.path.join(site_path, special_key, registry_id, f"{numerator}.txt")
        if not os.path.exists(barcode_file_path):
            if special_key not in VALID_SPECIAL_KEYS:
                VALID_SPECIAL_KEYS.append(special_key)
            return create_barcode_file(site_id, special_key, registry_id, numerator)
        else:
            return True

    site_path = os.path.join(BARCODE_DIR, site_id)
    if not os.path.isdir(site_path):
        print(f"LOCAL VALIDATION FAILED: Site directory '{site_path}' does not exist.")
        return False

    if special_key not in VALID_SPECIAL_KEYS:
        print(f"LOCAL VALIDATION FAILED: '{special_key}' is not a valid special key.")
        return False

    barcode_file_path = os.path.join(site_path, special_key, registry_id, f"{numerator}.txt")
    if os.path.exists(barcode_file_path):
        print("LOCAL VALIDATION FAILED: This receipt numerator has already been logged for this registry.")
        return False

    return create_barcode_file(site_id, special_key, registry_id, numerator)


# ==============================================================================
# --- SERVER-SIDE LOGIC ---
# ==============================================================================

class Server:
    def __init__(self):
        # The server no longer needs a button; it relies on the client's message.
        os.makedirs(BARCODE_DIR, exist_ok=True)

    def start(self):
        class BarcodeTCPHandler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    # Expect data in "barcode:button_state" format
                    message = self.request.recv(1024).strip().decode("utf-8")
                    if not message: return
                    
                    parts = message.split(':')
                    barcode = parts[0]
                    # The client sends "True" or "False" as a string
                    button_is_pressed = len(parts) > 1 and parts[1] == "True"

                    print(f"NETWORK REQUEST from {self.client_address[0]}: Barcode={barcode}, Override={button_is_pressed}")
                    
                    is_valid = process_barcode_locally(barcode, button_is_pressed)
                    self.request.sendall(b"open" if is_valid else b"close")
                except Exception as e:
                    print(f"An unexpected error in handler: {e}")

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            pass

        with ThreadedTCPServer(('0.0.0.0', SERVER_PORT), BarcodeTCPHandler) as server_instance:
            print(f"--- SERVER PROCESS STARTED --- Listening on all interfaces on port {SERVER_PORT}")
            server_instance.serve_forever()


# ==============================================================================
# --- CLIENT-SIDE LOGIC ---
# ==============================================================================

class Client:
    def __init__(self):
        self.gate = None
        self.config_button = None
        try:
            self.gate = LED(GATE_LED_PIN, active_high=True, initial_value=False)
            self.config_button = Button(CONFIG_BUTTON_PIN, pull_up=True)
        except Exception as e:
            print(f"WARNING (Client): Could not initialize GPIO pins. Error: {e}")

    # ADDED: Database deletion feature
    def delete_database(self):
        """Selectively deletes items from the database, sparing items in FILES_TO_KEEP."""
        print("\n--- WARNING: Config button held. Performing selective database cleanup... ---")
        
        if not os.path.isdir(BARCODE_DIR):
            print("Database directory not found, nothing to delete.")
            return

        for item_name in os.listdir(BARCODE_DIR):
            if item_name not in FILES_TO_KEEP:
                full_path = os.path.join(BARCODE_DIR, item_name)
                try:
                    if os.path.isdir(full_path):
                        print(f"Deleting data directory: {item_name}")
                        shutil.rmtree(full_path)
                    else:
                        print(f"Deleting data file: {item_name}")
                        os.remove(full_path)
                except OSError as e:
                    print(f"--- ERROR: Could not delete {item_name}. {e} ---")

        print("--- Selective cleanup complete. ---")
        self.play_sound("beep")

    def play_sound(self, sound_name):
        mp3_file = SOUND_PATH.format(sound_name)
        if os.path.exists(mp3_file):
            subprocess.Popen(["mpg123", "-a", "hw:2,0", mp3_file])

    def ping_server(self, ip):
        try:
            result = subprocess.run(["timeout", str(PING_TIMEOUT), 'ping', '-c', '1', ip], capture_output=True, text=True, check=False)
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def open_gate(self):
        if self.gate:
            self.gate.blink(on_time=0.2, off_time=0, n=1)
            print("Gate opened.")
        else:
            print("Gate control disabled (GPIO not initialized).")

    # MODIFIED: Now sends button state to server
    def send_data(self, data, button_is_pressed):
        if not self.ping_server(SERVER_IP):
            print("Server unreachable. Switching to OFFLINE mode.")
            is_valid_locally = process_barcode_locally(data, button_is_pressed)
            if is_valid_locally:
                self.open_gate()
                self.play_sound("sound")
            else:
                self.play_sound("beep")
            return

        print("Server online. Sending data for validation...")
        response = ""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
                client_socket.connect((SERVER_IP, SERVER_PORT))
                # Send data in "barcode:button_state" format
                message = f"{data}:{button_is_pressed}"
                client_socket.sendall(message.encode("utf-8"))
                response = client_socket.recv(1024).decode("utf-8")
                print(f"Server responded: {response}")
        except socket.error as e:
            print(f"Socket error: {e}")
            return

        if response == "open":
            self.open_gate()
            self.play_sound("sound")
        elif response == "close":
            self.play_sound("beep")

    def get_scanner(self):
        for vendor_id, product_id in USB_VENDORS:
            dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
            if dev: return dev
        return None

    def reader_process(self, queue):
        KEYCODE_MAP = {30: '1', 31: '2', 32: '3', 33: '4', 34: '5', 35: '6', 36: '7', 37: '8', 38: '9', 39: '0'}
        dev = None
        while True:
            if dev is None:
                dev = self.get_scanner()
                if dev is None:
                    print("Scanner not found. Retrying in 5 seconds...")
                    time.sleep(5)
                    continue
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
                dev.set_configuration()
                ep = dev[0].interfaces()[0].endpoints()[0]
                eaddr = ep.bEndpointAddress
                barcode_chars = []
                while True:
                    data = dev.read(eaddr, ep.wMaxPacketSize, timeout=0)
                    if not data or len(data) < 3 or data[2] == 0: continue
                    keycode = data[2]
                    if keycode == 40: # Enter
                        barcode = "".join(barcode_chars)
                        print(f"Barcode scanned: {barcode}")
                        queue.put(barcode)
                        barcode_chars = []
                    else:
                        char = KEYCODE_MAP.get(keycode)
                        if char: barcode_chars.append(char)
            except usb.core.USBError as e:
                dev = None
                time.sleep(1)

    # MODIFIED: Main loop now includes all requested features
    def start(self):
        print(f"--- CLIENT PROCESS STARTED ---")
        queue = multiprocessing.Queue()
        reader = multiprocessing.Process(target=self.reader_process, args=(queue,))
        reader.daemon = True
        reader.start()

        last_barcode = None
        button_held_start_time = None
        db_deleted_this_hold = False
        try:
            while True:
                # --- Handle Scanned Barcodes ---
                if not queue.empty():
                    barcode = queue.get()
                    button_is_pressed = self.config_button and self.config_button.is_pressed
                    is_master_key = barcode.startswith(MASTER_KEY_PREFIX) and barcode.endswith(MASTER_KEY_SUFFIX)

                    if is_master_key or button_is_pressed:
                        print("Master Key or Override Button scan, bypassing duplicate check.")
                        self.send_data(barcode, button_is_pressed)
                        if not is_master_key:
                            last_barcode = barcode
                    elif barcode != last_barcode:
                        self.send_data(barcode, button_is_pressed)
                        last_barcode = barcode
                    else:
                        print(f"Duplicate scan ignored: {barcode}")
                        self.play_sound("beep") # Play sound for ignored scan

                # --- Handle 10-Second Button Hold for Deletion ---
                if self.config_button and self.config_button.is_pressed:
                    if button_held_start_time is None:
                        button_held_start_time = time.time()
                        db_deleted_this_hold = False
                    hold_duration = time.time() - button_held_start_time
                    if hold_duration >= 10 and not db_deleted_this_hold:
                        self.delete_database()
                        db_deleted_this_hold = True
                else:
                    button_held_start_time = None

                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            if reader.is_alive():
                reader.terminate()
            if self.gate:
                self.gate.off()
            print("Client process cleaned up.")


# ==============================================================================
# --- MAIN EXECUTION ---
# ==============================================================================

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

if __name__ == "__main__":
    local_ip = get_local_ip()
    print(f"Local IP detected: {local_ip}")

    server_process = None

    if local_ip == SERVER_IP:
        print("This machine is the designated server. Starting server process in background.")
        server_app = Server()
        server_process = multiprocessing.Process(target=server_app.start)
        server_process.daemon = True
        server_process.start()

    # Every machine runs the client logic.
    print("Starting client logic.")
    client_app = Client()
    try:
        client_app.start()
    except KeyboardInterrupt:
        print("\nShutting down main process...")
    finally:
        if server_process and server_process.is_alive():
            print("Terminating server process...")
            server_process.terminate()
            server_process.join()
        print("System shutdown complete.")