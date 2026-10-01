import {MigrationInterface, QueryRunner} from "typeorm";

export class AddColumns1700000000002 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`ALTER TABLE "schedule" ADD "notes" character varying`);
        await queryRunner.query(`ALTER TABLE "schedule" ADD "seats" integer NOT NULL`);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
