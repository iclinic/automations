<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * `DROP TABLE` em `up()`, no dialeto PostgreSQL e com o nome qualificado por
 * schema. Par da fixture MySQL de mesmo propósito: a mesma operação nas duas
 * bases do consumidor tem que sair com a mesma severidade e a mesma frase.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120200 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Descarta a tabela de posicoes de armazem';
    }

    public function up(Schema $schema): void
    {
        $this->addSql('DROP TABLE stock."warehouse_bin"');
    }

    public function down(Schema $schema): void
    {
        $this->addSql('CREATE TABLE stock."warehouse_bin" ("id" SERIAL PRIMARY KEY)');
    }
}
