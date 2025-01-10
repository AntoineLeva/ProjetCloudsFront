from datetime import datetime
import json
import os
import shutil
import subprocess
import threading

from flask import Flask, app, jsonify, request
from flask_cors import CORS
import paramiko

from appV5 import ssh_run_command

app = Flask(__name__)
CORS(app)

# Récupération des paramètres de la requête
# vm_ip = '192.168.66.192'  # IP de la VM
# user = 'imt'  # Utilisateur de la VM
# password = 'admin'  # Mot de passe de la VM
# repo_url = 'https://github.com/AntoineLeva/ProjetClouds.git'  # URL du dépôt
# temp_clone_dir = os.path.join(os.getcwd(), 'temp_repo')  # Dossier temporaire pour cloner le dépôt

def extract_repo_info(repo_url):
    """
    Extraire le nom de l'utilisateur et le nom du dépôt à partir de l'URL Git.
    Exemple : https://github.com/AntoineLeva/ProjetClouds.git -> ('AntoineLeva', 'ProjetClouds')
    """
    repo_parts = repo_url.strip('/').split('/')
    user = repo_parts[-2]  # L'utilisateur est l'avant-dernier élément
    repo_name = repo_parts[-1].replace('.git', '')  # Le nom du dépôt sans l'extension .git
    return user+"_"+repo_name+".json"

class PipelineData:
    def __init__(self, vm_ip, user, password, repo_url):
        self.vm_ip = vm_ip
        self.user = user
        self.password = password
        self.repo_url = repo_url
        parent_dir = os.path.dirname(os.getcwd())
        self.temp_clone_dir = os.path.join(parent_dir, 'temp_repo')  # Dossier temporaire pour cloner le dépôt

class Step:
    def __init__(self, name, function):
        self.name = name
        self.function = function
        self.status = "pending"  # pending, running, success, failed
    
    def run(self, data):
        try:
            self.status = "running"
            self.function(data)
            self.status = "success"
            return True
        except Exception as e:
            self.status = "failed"
            print(f"Step '{self.name}' failed: {e}")
            return False

def remove_readonly(func, path, _):
    """Change les permissions et supprime les fichiers en lecture seule."""
    os.chmod(path, 0o777)
    func(path)

def scp_directory(vm_ip, user, password, local_dir, remote_dir):
    """Copie un répertoire vers la VM via SCP avec tout son contenu récursivement."""
    try:
        transport = paramiko.Transport((vm_ip, 22))
        transport.connect(username=user, password=password)

        sftp = paramiko.SFTPClient.from_transport(transport)

        for root, dirs, files in os.walk(local_dir):
            # Calculer le chemin distant
            relative_path = os.path.relpath(root, local_dir)
            remote_path = os.path.join(remote_dir, relative_path).replace("\\", "/")  # Compatibilité Unix

            # Créer les répertoires distants de manière récursive
            try:
                sftp.stat(remote_path)  # Vérifie si le répertoire existe déjà
            except FileNotFoundError:
                parts = remote_path.split("/")
                for i in range(1, len(parts) + 1):
                    partial_path = "/".join(parts[:i])
                    try:
                        sftp.mkdir(partial_path)
                        print(f"Répertoire distant créé : {partial_path}")
                    except IOError:
                        pass  # Si le répertoire existe déjà, ignorer

            # Transférer les fichiers du répertoire courant
            for file in files:
                local_file_path = os.path.join(root, file)
                remote_file_path = os.path.join(remote_path, file).replace("\\", "/")
                print(f"Transfert fichier : {local_file_path} --> {remote_file_path}")
                sftp.put(local_file_path, remote_file_path)

        sftp.close()
        transport.close()
        print("Transfert complet du répertoire avec succès.")
        return True
    except Exception as e:
        print(f"Erreur lors du transfert du répertoire via SCP : {e}")
        raise e

# Étape 1 : Clonage du dépôt GitHub
def clone_repository(data):
    if os.path.exists(data.temp_clone_dir):
        print(f"Suppression du dossier temporaire : {data.temp_clone_dir}")
        shutil.rmtree(data.temp_clone_dir, onerror=remove_readonly)

    print(f"Clonage du dépôt {data.repo_url} dans {data.temp_clone_dir}...")
    os.system(f'git clone "{data.repo_url}" "{data.temp_clone_dir}"')
    if not os.path.isdir(data.temp_clone_dir):
        raise Exception("Clonage du dépôt échoué.")

# Étape 2 : Vérifier les TU
def verif_TU(data):
    print(f"Vérficiation des tests unitaires")
    maven_command = f'mvn test -f "{os.path.join(data.temp_clone_dir, "pom.xml")}"'
    
    try:
        exit_code = os.system(maven_command)

        if (exit_code == 0):
            print("Tests réussis !")
        else:
            raise Exception("Tests unitaire non passés.")
            
    except Exception as e:
        raise Exception("Tests unitaire non passés.")

# Étape 3 : Transfert vers la VM
def transfer_to_vm(data):
    print(f"Copie du dépôt cloné vers la VM {data.vm_ip}...")
    success = scp_directory(data.vm_ip, data.user, data.password, data.temp_clone_dir, f"/home/{data.user}/repo")
    if not success:
        raise Exception("Transfert vers la VM échoué.")

def stop_delete_docker_container(data):
     # Étape 4 : Arrêter et supprimer les conteneurs existants sur la VM
    print(f"Arrêt et suppression des conteneurs en cours sur la VM {data.vm_ip}...")
    stop_command = "docker ps -q | xargs -r docker stop"
    remove_command = "docker ps -a -q | xargs -r docker rm"
    ssh_run_command(data.vm_ip, data.user, data.password, stop_command)
    ssh_run_command(data.vm_ip, data.user, data.password, remove_command)

def create_backup(data):
     # Étape 5 : Créer un backup de l'image existante
    print(f"Création d'un backup de l'image 'repo_app' sur la VM {data.vm_ip}...")
    ssh_run_command(data.vm_ip, data.user, data.password, "mkdir -p /home/imt/backupup")  # Créer le dossier de backup

    # Vérifier si l'image existe avant de sauvegarder
    check_image_cmd = "docker images -q repo_app"
    image_id, _ = ssh_run_command(data.vm_ip, data.user, data.password, check_image_cmd)

    if image_id.strip():  # Si l'image existe
        backup_cmd = f"docker save {image_id.strip()} -o /home/imt/backupup/repo_app_backup.tar"
        backup_output, backup_error = ssh_run_command(data.vm_ip, data.user, data.password, backup_cmd)
        print("Backup output : ", backup_output)
        print("Backup error : ", backup_error)

        if backup_error:
            raise Exception("Erreur lors de la sauvegarde de l'image.")
    else:
        print("Aucune image 'repo_app' trouvée pour backup.")

def delete_image_copy(data):
    # Vérifier si l'image existe avant de sauvegarder
    check_image_cmd = "docker images -q repo_app"
    image_id, _ = ssh_run_command(data.vm_ip, data.user, data.password, check_image_cmd)

    # Étape 6 : Supprimer l'image Docker existante
    if image_id.strip():  # supp si pb 
        print(f"Suppression de l'image Docker 'repo_app'...")
        remove_image_cmd = f"docker rmi -f {image_id.strip()}"
        remove_output, remove_error = ssh_run_command(data.vm_ip, data.user, data.password, remove_image_cmd)
        print("Remove output : ", remove_output)
        print("Remove error : ", remove_error)

def lauch_docker_compose(data):
    # Étape 7 : Lancer Docker Compose sur la VM
    print(f"Lancement de Docker Compose sur la VM {data.vm_ip}...")
    compose_cmd = f"cd /home/{data.user}/repo && docker-compose up -d --build"
    compose_output, compose_error = ssh_run_command(data.vm_ip, data.user, data.password, compose_cmd)
    print("Compose output : ", compose_output)
    print("Compose error : ", compose_error)

def sonar_qube(data):
    print(f"Vérficiation des tests sonarQube")
    maven_command = f'mvn sonar:sonar -Dsonar.host.url=http://localhost:9000/ -Dsonar.login=sqa_ef1af10d966155e1d893438d8d7b3e23a97dd168 -X -f ../temp_repo/pom.xml'
    
    try:
        exit_code = os.system(maven_command)

        if (exit_code == 0):
            print("Tests réussis sonar!")
        else:
            raise Exception("Tests sonar non passés.")
            
    except Exception as e:
        raise Exception("Tests sonar non passés.")

# Définition des étapes
steps = [
    Step("1-Cloner le dépôt GitHub", clone_repository),
    Step("2-Vérification de SonarQube", sonar_qube),
    Step("3-Vérifier les tests unitaires", verif_TU),
    Step("4-Transfert du code sur la VM", transfer_to_vm),
    Step("5-Arrêt et suppression des conteneurs en cours sur la VM", stop_delete_docker_container),
    Step("6-Créer un backup de l'image existante", create_backup),
    Step("7-Supprimer l'ancienne image", delete_image_copy),
    Step("8-Lancement de Docker Compose sur la VM", lauch_docker_compose),
]

pipelines_dir = 'pipelines'

class Pipeline:
    def __init__(self, steps, state_file="pipeline_state.json"):
        self.steps = steps
        self.state_file = state_file
        
    def save_state(self):
        try:
            with open(self.state_file, "r") as f:
                pipeline = json.load(f)

            creation_date = pipeline["datas"]["creation_date"]
            last_run_date = pipeline["datas"]["last_run_date"]
            if last_run_date != "":
                pipeline["logs"][last_run_date] = {step.name: step.status for step in self.steps}
            else:
                pipeline["logs"][creation_date] = {step.name: step.status for step in self.steps}

            with open(self.state_file, "w") as f:
                json.dump(pipeline, f, indent=4)
        except FileNotFoundError:
            pass

    def load_state(self):
        # try:
            # with open(self.state_file, "r") as f:
            #     pipeline = json.load(f)
                # if date == "":
        for step in self.steps:
            step.status = "pending"
                # else:
                    # log = pipeline["logs"][date]
                    # for step in self.steps:
                    #     step.status = log[step.name]
        # except FileNotFoundError:
        #     pass

    def run(self, data):
        try:
            with open(self.state_file, "r+") as f:
                pipeline = json.load(f)
                
            last_run_date = pipeline["datas"]["last_run_date"]
            self.load_state()
            date_actuelle = datetime.now()
            date = date_actuelle.strftime("%Y-%m-%d %H:%M:%S")
            pipeline["datas"]["last_run_date"] = date

            with open(self.state_file, "w") as f:
                json.dump(pipeline, f, indent=4)

            self.save_state()
        except FileNotFoundError:
            pass
        dontstoptest = True
        for step in self.steps:
            if (step.status in ["pending", "failed"]) and dontstoptest:
                # try:
                dontstoptest = step.run(data)
                self.save_state()

def run_process(repo_url, data):
    try:
        file_name = os.path.join(os.path.join(os.getcwd(),'pipelines'), extract_repo_info(repo_url))

        pipeline = Pipeline(steps, file_name)
        pipeline.run(data)
    except Exception as e:
        print(f"Erreur dans le processus de pipeline pour {repo_url}: {e}")

# Fonction utilitaire pour lire l'état de la pipeline
def get_pipeline_state(file_name):
    if os.path.exists(file_name):
        with open(file_name, "r") as f:
            return json.load(f)
    return {}  # Si le fichier n'existe pas, retourne un état vide

@app.route('/create-pipeline', methods=['POST'])
def create_pipeline():
    # Récupérer les données de la requête
    data = request.json or {}
    vm_ip = data.get('vm_ip', '192.168.66.192')
    user = data.get('user', 'imt')
    password = data.get('password', 'admin')
    repo_url = data.get('repo_url', 'https://github.com/AntoineLeva/ProjetClouds.git')

    file_name = extract_repo_info(repo_url)
    
    date_actuelle = datetime.now()
    date_cle = date_actuelle.strftime("%Y-%m-%d %H:%M:%S")
    pipeline = {
        "datas": {
            "creation_date": date_cle,
            "vm_ip": vm_ip,
            "user": user,
            "password": password,
            "repo_url": repo_url,
            "last_run_date": "",
        },
        "logs": {}
    }

    if not os.path.exists(pipelines_dir):
        os.makedirs(pipelines_dir)

    with open(os.path.join(pipelines_dir, file_name), "w") as f:
        json.dump(pipeline, f, indent=4)
    
    return jsonify({"message": "Pipeline crée"})

@app.route('/start-pipeline', methods=['POST'])
def start_pipeline():
    # Récupérer les données de la requête
    data = request.json or {}
    vm_ip = data.get('vm_ip', '192.168.66.192')
    user = data.get('user', 'imt')
    password = data.get('password', 'admin')
    repo_url = data.get('repo_url', 'https://github.com/AntoineLeva/ProjetClouds.git')

    # Créer un objet PipelineData avec les informations
    pipeline_data = PipelineData(vm_ip, user, password, repo_url)

    # Lancer le processus dans un thread séparé
    thread = threading.Thread(target=run_process, args=(repo_url, pipeline_data))
    thread.start()

    return jsonify({"message": "Processus démarré"})

@app.route('/get-all-pipelines', methods=['POST'])
def get_all_pipelines():
    all_data = []

    # Vérifie si le dossier existe
    if not os.path.exists(pipelines_dir):
        return {"error": "Le dossier 'pipelines' n'existe pas."}

    # Parcours tous les fichiers dans le dossier 'pipelines'
    for file_name in os.listdir(pipelines_dir):
        file_path = os.path.join(pipelines_dir, file_name)
        if os.path.isfile(file_path) and file_name.endswith('.json'):
            try:
                # Lecture du contenu JSON
                with open(file_path, "r") as f:
                    data = json.load(f)

                # Récupère le dernier log s'il existe
                if "logs" in data:
                    logs = data["logs"]
                    if logs:
                        # Trie les logs par date et récupère le dernier
                        last_log_date = max(logs.keys(), key=lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M:%S"))
                        last_log = {
                            "date": last_log_date,
                            "log": logs[last_log_date]
                        }
                    else:
                        last_log = None
                else:
                    last_log = None

                # Ajoute les données et le dernier log
                data_with_last_log = {
                    "datas": data.get("datas", {}),
                    # "logs": data.get("logs", {}),
                    "last_log": last_log
                }
                all_data.append(data_with_last_log)
            except Exception as e:
                print(f"Erreur de lecture du fichier {file_name}: {e}")

    return {"pipelines": all_data}

def get_pipeline_logs(file_name):
    try:
        # Lire le contenu du fichier JSON
        with open(file_name, 'r') as file:
            pipeline = json.load(file)
        
        # Vérifier si les logs existent dans le fichier
        if 'logs' in pipeline:
            return pipeline['logs']
        else:
            raise ValueError("Aucun log trouvé dans ce pipeline.")
    except FileNotFoundError:
        print(f"Erreur : Le fichier '{file_name}' est introuvable.")
    except json.JSONDecodeError:
        print(f"Erreur : Le fichier '{file_name}' n'est pas un fichier JSON valide.")
    except Exception as e:
        print(f"Erreur : {e}")


@app.route('/get-pipeline-details', methods=['GET'])
def get_pipeline_details():
    pipeline_id = request.args.get('id')

    
    fichier = pipeline_id+".json"
    file = os.path.join(os.getcwd(),pipelines_dir)
    file_path = os.path.join(file, fichier)
 
    pipeline = get_pipeline_logs(file_path)
    
    if pipeline:
        return jsonify(pipeline)
    else:
        return jsonify({"error": "Pipeline introuvable"}), 404

@app.route('/get-pipeline-status', methods=['POST'])
def get_pipeline_status():
    data = request.json or {}
    vm_ip = data.get('vm_ip', '192.168.66.192')
    user = data.get('user', 'imt')
    password = data.get('password', 'admin')
    repo_url = data.get('repo_url', 'https://github.com/AntoineLeva/ProjetClouds.git')

    file_name = extract_repo_info(repo_url)

    state = get_pipeline_state(file_name)
    return jsonify(state)

if __name__ == '__main__':
    app.run(debug=True, port=5001)