#!/bin/bash

# Repository list
repos=()
while IFS= read -r line; do
    repos+=("$line")
done < "repositories-list.txt"

# Template path
script_dir=$(dirname "$(realpath "$0")")
template1_path="$script_dir/gitstream.cm"
template2_path="$script_dir/gitstream.yml"
issue_key=""

reset(){
    for repo in "${repos[@]}"; do
        repo_name=$(basename "$repo" .git)
        echo "Removing directory $repo_name"
        if [ -d "$repo_name" ]; then
            echo "Removing directory $repo_name"
            rm -rf "$repo_name"
        fi
    done
}


# Get Jira Issue Key from user input
get_user_input() {
    read -p "Enter the Jira issue key for creating branches in the repositories to be updated: " issue_key
    echo $issue_key
}

# Update repository
update_repo() {

    repo_url=$1
    repo_name=$(basename "$repo_url" .git)

    # Clone the repository
    git clone "$repo_url"

    # Enter the repository directory
    cd "$repo_name" || exit

    # Check if the file exists, if not, create the directories and the file
    if [ ! -f ".github/workflows/gitstream.yml" ]; then
        mkdir -p .github/workflows
    fi
    if [ ! -f ".cm/gitstream.cm" ]; then
        mkdir -p .cm
    fi

    # Remove all YAML files with 'jira' prefix in the .github/workflows folder
    cp "$template1_path" .cm/gitstream.cm
    cp "$template2_path" .github/workflows/gitstream.yml

    # Create a new branch with the issue key name
    git pull
    git checkout -b feature/"$issue_key"
    git add .
    git commit -am "Adicionando arquivo de configuração para integração com o gitstream"
    git push origin feature/"$issue_key"

    # Create pull request
    pr_title="[$issue_key] LinearB <> Gitstream integration"
    pr_body=$(cat <<EOF
# Ticket
[$issue_key](https://afya-spm.atlassian.net/browse/$issue_key)

# Description
Configuração para integração do LinearB com o Gitstream nos repositórios
EOF
)
    pr_url=$(gh pr create --title "$pr_title" --body "$pr_body" --base "$main_branch" --head feature/"$issue_key")
    echo "Pull request created: $pr_url"
    pr_urls+=("$pr_url")


    # Go back to the previous directory
    cd ..

    echo "Repository $repo_name updated."
}


reset

get_user_input

# Iterate over the list of repositories
for repo in "${repos[@]}"; do
    update_repo "$repo"
done

echo "All repositories have been updated."
echo "Links for the pull requests:"
for pr_url in "${pr_urls[@]}"; do
    echo "$pr_url"
done
